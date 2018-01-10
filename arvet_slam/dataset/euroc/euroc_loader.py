# Copyright (c) 2017, John Skinner
import os.path
import typing
import numpy as np
import xxhash
import cv2
import yaml
try:
    from yaml import CDumper as YamlDumper, CLoader as YamlLoader
except ImportError:
    from yaml import Dumper as YamlDumper, Loader as YamlLoader


import arvet.util.image_utils as image_utils
import arvet.util.associate as ass
import arvet.metadata.camera_intrinsics as cam_intr
import arvet.metadata.image_metadata as imeta
import arvet.util.transform as tf
import arvet.core.image_entity
import arvet.image_collections.image_collection_builder


def make_camera_pose(tx: float, ty: float, tz: float, qw: float, qx: float, qy: float, qz: float) -> tf.Transform:
    """
    As far as I can tell, EuRoC uses Z forward coordinates, the same as everything else.
    I need to switch it to X-forward coordinates.

    :param tx: The x coordinate of the location
    :param ty: The y coordinate of the location
    :param tz: The z coordinate of the location
    :param qw: The scalar part of the quaternion orientation
    :param qx: The x imaginary part of the quaternion orientation
    :param qy: The y imaginary part of the quaternion orientation
    :param qz: The z imaginary part of the quaternion orientation
    :return: A Transform object representing the world pose of the current frame
    """
    return tf.Transform(
        location=(tz, -tx, -ty),
        rotation=(qw, qz, -qx, -qy),
        w_first=True
    )


def read_image_filenames(images_file_path: str) -> typing.Mapping[int, str]:
    """
    Read data from a camera sensor, formatted as a csv,
    producing timestamp, filename pairs.
    :param images_file_path:
    :return:
    """
    filename_map = {}
    with open(images_file_path, 'r') as images_file:
        for line in images_file:
            if line.startswith('#'):
                # This line is a comment
                continue
            parts = line.split(',')
            if len(parts) >= 2:
                timestamp, relative_path = parts[0:2]
                filename_map[int(timestamp)] = relative_path.rstrip()  # To remove trailing newlines
    return filename_map


def read_trajectory(trajectory_filepath: str) -> typing.Mapping[int, tf.Transform]:
    """
    Read the ground-truth camera trajectory from file.
    The raw pose information is relative to some world frame, we adjust it to be relative to the initial pose
    of the camera, for standardization.
    This trajectory describes the motion of the robot, combine it with the pose of the camera relative to the robot
    to get the camera trajectory.

    :param trajectory_filepath:
    :return: A map of timestamp to camera pose.
    """
    trajectory = {}
    first_pose = None
    with open(trajectory_filepath, 'r') as trajectory_file:
        for line in trajectory_file:
            if line.startswith('#'):
                # This line is a comment, skip and continue
                continue
            parts = line.split(',')
            if len(parts) >= 8:
                timestamp, tx, ty, tz, qw, qx, qy, qz = parts[0:8]
                pose = make_camera_pose(float(tx), float(ty), float(tz),
                                        float(qw), float(qx), float(qy), float(qz))
                # Find the pose relative to the first frame, which we fix as 0,0,0
                if first_pose is None:
                    first_pose = pose
                    trajectory[int(timestamp)] = tf.Transform()
                else:
                    trajectory[int(timestamp)] = first_pose.find_relative(pose)
    return trajectory


def associate_data(root_map: typing.Mapping, *args: typing.Mapping) -> typing.List[typing.List]:
    """
    Convert a number of maps key->value to a list of lists
    [[key, map1[key], map2[key] map3[key] ...] ...]

    The list will be sorted in key order
    Returned inner lists will be in the same order as they are passed as arguments.

    The first map passed is considered the reference point for the list of keys,
    :param root_map: The first map to associate
    :param args: Additional maps to associate to the first one
    :return:
    """
    if len(args) <= 0:
        # Nothing to associate, flatten the root map and return
        return sorted([k, v] for k, v in root_map.items())
    root_keys = set(root_map.keys())
    all_same = True
    # First, check if all the maps have the same list of keys
    for other_map in args:
        if set(other_map.keys()) != root_keys:
            all_same = False
            break
    if all_same:
        # All the maps have the same set of keys, just flatten them
        return sorted([key, root_map[key]] + [other_map[key] for other_map in args]
                      for key in root_keys)
    else:
        # We need to associate the maps, the timestamps are a little out
        rekeyed_maps = []
        for other_map in args:
            matches = ass.associate(root_map, other_map, offset=0, max_difference=3)
            rekeyed_map = {root_key: other_map[other_key] for root_key, other_key in matches}
            root_keys &= set(rekeyed_map.keys())
            rekeyed_maps.append(rekeyed_map)
        return sorted([key, root_map[key]] + [rekeyed_map[key] for rekeyed_map in rekeyed_maps]
                      for key in root_keys)


def get_camera_calibration(sensor_yaml_path: str) -> typing.Tuple[tf.Transform, cam_intr.CameraIntrinsics]:
    with open(sensor_yaml_path, 'r') as sensor_file:
        sensor_data = yaml.load(sensor_file, YamlLoader)

    d = sensor_data['T_BS']['data']
    extrinsics = tf.Transform(np.array([
        [d[0], d[1], d[2], d[3]],
        [d[4], d[5], d[6], d[7]],
        [d[8], d[9], d[10], d[11]],
        [d[12], d[13], d[14], d[15]],
    ]))
    resolution = sensor_data['resolution']
    intrinsics = cam_intr.CameraIntrinsics(
        width=resolution[0],
        height=resolution[1],
        fx=sensor_data['intrinsics'][0],
        fy=sensor_data['intrinsics'][1],
        cx=sensor_data['intrinsics'][2],
        cy=sensor_data['intrinsics'][3],
        k1=sensor_data['distortion_coefficients'][0],
        k2=sensor_data['distortion_coefficients'][1],
        p1=sensor_data['distortion_coefficients'][2],
        p2=sensor_data['distortion_coefficients'][3]
    )
    return extrinsics, intrinsics


def rectify(left_extrinsics: tf.Transform, left_intrinsics: cam_intr.CameraIntrinsics,
            right_extrinsics: tf.Transform, right_intrinsics: cam_intr.CameraIntrinsics) -> \
        typing.Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """
    Compute mapping matrices for performing stereo rectification, from the camera properties.
    Applying the returned transformation to the images using cv2.remap gives us undistorted stereo rectified images

    :param left_extrinsics:
    :param left_intrinsics:
    :param right_extrinsics:
    :param right_intrinsics:
    :return: 4 remapping matrices: left x, left y, right x, right y
    """
    shape = (left_intrinsics.width, left_intrinsics.height)
    left_distortion = np.array([
        left_intrinsics.k1, left_intrinsics.k2,
        left_intrinsics.p1, left_intrinsics.p2,
        left_intrinsics.k3
    ])
    right_distortion = np.array([
        right_intrinsics.k1, right_intrinsics.k2,
        right_intrinsics.p1, right_intrinsics.p2,
        right_intrinsics.k3
    ])
    relative_transform = left_extrinsics.find_relative(right_extrinsics).transform_matrix

    result = cv2.stereoRectify(left_intrinsics.intrinsic_matrix(), left_distortion,
                               right_intrinsics.intrinsic_matrix(), right_distortion, shape,
                               relative_transform[0:3, 0:3], relative_transform[0:3, 3])
    r_left, r_right, p_left, p_right = result[0:4]

    m1l, m2l = cv2.initUndistortRectifyMap(left_intrinsics.intrinsic_matrix(), left_distortion, r_left,
                                           p_left[0:3, 0:3], shape, cv2.CV_32F)
    m1r, m2r = cv2.initUndistortRectifyMap(right_intrinsics.intrinsic_matrix(), right_distortion, r_right,
                                           p_right[0:3, 0:3], shape, cv2.CV_32F)
    return m1l, m2l, m1r, m2r


def copy_undistored(instrinsics: cam_intr.CameraIntrinsics) -> cam_intr.CameraIntrinsics:
    """
    Duplicate a camera intrinsics object, without distortion, since that is eliminated in our rectification
    :param instrinsics:
    :return:
    """
    return cam_intr.CameraIntrinsics(
        width=instrinsics.width,
        height=instrinsics.height,
        fx=instrinsics.fx,
        fy=instrinsics.fy,
        cx=instrinsics.cx,
        cy=instrinsics.cy,
        skew=instrinsics.s
    )


def import_dataset(root_folder, db_client):
    """
    Load an Autonomous Systems Lab dataset into the database.
    See http://projects.asl.ethz.ch/datasets/doku.php?id=kmavvisualinertialdatasets#downloads

    Some information drawn from the ethz_asl dataset tools, see: https://github.com/ethz-asl/dataset_tools
    :param root_folder: The body folder, containing body.yaml (i.e. the extracted mav0 folder)
    :param db_client: The database client.
    :return:
    """
    if not os.path.isdir(root_folder):
        return None

    # Step 1: Read the meta-information from the files
    left_rgb_path = os.path.join(root_folder, 'cam0', 'data.csv')
    left_camera_intrinsics_path = os.path.join(root_folder, 'cam0', 'sensor.yaml')
    right_rgb_path = os.path.join(root_folder, 'cam1', 'data.csv')
    right_camera_intrinsics_path = os.path.join(root_folder, 'cam1', 'sensor.yaml')
    trajectory_path = os.path.join(root_folder, 'state_groundtruth_estimate0', 'data.csv')

    if (not os.path.isfile(left_rgb_path) or not os.path.isfile(left_camera_intrinsics_path) or
            not os.path.isfile(right_rgb_path) or not os.path.isfile(right_camera_intrinsics_path) or
            not os.path.isfile(trajectory_path)):
        # Stop if we can't find the metadata files within the directory
        return None

    left_image_files = read_image_filenames(left_rgb_path)
    left_extrinsics, left_intrinsics = get_camera_calibration(left_camera_intrinsics_path)
    right_image_files = read_image_filenames(left_rgb_path)
    right_extrinsics, right_intrinsics = get_camera_calibration(right_camera_intrinsics_path)
    trajectory = read_trajectory(trajectory_path)

    # Step 2: Create stereo rectification matrices from the intrinsics
    left_x, left_y, right_x, right_y = rectify(left_extrinsics, left_intrinsics, right_extrinsics, right_intrinsics)

    # Step 3: Associate the different data types by timestamp. Trajectory last because it's bigger than the stereo.
    all_metadata = associate_data(left_image_files, right_image_files, trajectory)

    # Step 4: Load the images from the metadata
    builder = arvet.image_collections.image_collection_builder.ImageCollectionBuilder(db_client)
    first_timestamp = None
    for timestamp, left_image_file, right_image_file, robot_pose in all_metadata:
        # Timestamps are in POSIX nanoseconds, re-zero them to the start of the dataset, and scale to seconds
        if first_timestamp is None:
            first_timestamp = timestamp
        timestamp = (timestamp - first_timestamp) / 1e9

        left_data = image_utils.read_colour(os.path.join(root_folder, 'cam0', 'data', left_image_file))
        right_data = image_utils.read_colour(os.path.join(root_folder, 'cam1', 'data', right_image_file))

        left_data = cv2.remap(left_data, left_x, left_y, cv2.INTER_LINEAR)
        right_data = cv2.remap(right_data, right_x, right_y, cv2.INTER_LINEAR)

        left_pose = robot_pose.find_independent(left_extrinsics)
        right_pose = robot_pose.find_independent(right_extrinsics)

        builder.add_image(image=arvet.core.image_entity.StereoImageEntity(
            left_data=left_data,
            right_data=right_data,
            metadata=imeta.ImageMetadata(
                hash_=xxhash.xxh64(left_data).digest(),
                camera_pose=left_pose,
                right_camera_pose=right_pose,
                intrinsics=copy_undistored(left_intrinsics),
                right_intrinsics=copy_undistored(right_intrinsics),
                source_type=imeta.ImageSourceType.REAL_WORLD,
                environment_type=imeta.EnvironmentType.INDOOR_CLOSE,
                light_level=imeta.LightingLevel.WELL_LIT,
                time_of_day=imeta.TimeOfDay.DAY,
            )
        ), timestamp=timestamp)
    return builder.save()
