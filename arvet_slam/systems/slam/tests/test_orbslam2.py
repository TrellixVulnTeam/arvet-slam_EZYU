# Copyright (c) 2017, John Skinner
import unittest
import unittest.mock as mock
import logging
import os.path
from pathlib import Path
from io import StringIO
import string
from queue import Empty as QueueEmpty
import shutil
import tempfile
import multiprocessing
import multiprocessing.queues
import numpy as np
import transforms3d as tf3d
from bson import ObjectId
from pymodm.context_managers import no_auto_dereference

import arvet.database.tests.database_connection as dbconn
from arvet.util.transform import Transform
from arvet.util.test_helpers import ExtendedTestCase
from arvet.config.path_manager import PathManager
import arvet.metadata.image_metadata as imeta
from arvet.metadata.camera_intrinsics import CameraIntrinsics
from arvet.core.sequence_type import ImageSequenceType
from arvet.core.image_source import ImageSource
from arvet.core.image import Image, StereoImage
from arvet.core.system import VisionSystem

from arvet_slam.trials.slam.tracking_state import TrackingState
from arvet_slam.trials.slam.visual_slam import SLAMTrialResult, FrameResult
from arvet_slam.systems.slam.orbslam2 import OrbSlam2, SensorMode, dump_config, nested_to_dotted, \
    make_relative_pose, run_orbslam

_temp_folder = 'temp-test-orbslam2'


class TestOrbSlam2Database(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        dbconn.connect_to_test_db()

    def setUp(self):
        # Remove the collection as the start of the test, so that we're sure it's empty
        VisionSystem._mongometa.collection.drop()

    @classmethod
    def tearDownClass(cls):
        # Clean up after ourselves by dropping the collection for this model
        VisionSystem._mongometa.collection.drop()
        if os.path.isfile(dbconn.image_file):
            os.remove(dbconn.image_file)

    def test_stores_and_loads(self):
        obj = OrbSlam2(
            vocabulary_file='im-a-file-{0}'.format(np.random.randint(0, 100000)),
            mode=np.random.choice([SensorMode.MONOCULAR, SensorMode.STEREO, SensorMode.RGBD]),
            depth_threshold=np.random.uniform(10, 100),
            depthmap_factor=np.random.uniform(0.1, 2),
            orb_num_features=np.random.randint(10, 10000),
            orb_scale_factor=np.random.uniform(0.5, 2),
            orb_num_levels=np.random.randint(3, 20),
            orb_ini_threshold_fast=np.random.randint(10, 20),
            orb_min_threshold_fast=np.random.randint(3, 10)
        )
        obj.save()

        # Load all the entities
        all_entities = list(VisionSystem.objects.all())
        self.assertGreaterEqual(len(all_entities), 1)
        self.assertEqual(all_entities[0], obj)
        all_entities[0].delete()

    def test_stores_and_loads_minimal_args(self):
        obj = OrbSlam2(
            vocabulary_file='im-a-file-{0}'.format(np.random.randint(0, 100000)),
            mode=np.random.choice([SensorMode.MONOCULAR, SensorMode.STEREO, SensorMode.RGBD])
        )
        obj.save()

        # Load all the entities
        all_entities = list(VisionSystem.objects.all())
        self.assertGreaterEqual(len(all_entities), 1)
        self.assertEqual(all_entities[0], obj)
        all_entities[0].delete()


class TestOrbSlam2(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        logging.disable(logging.CRITICAL)
        os.makedirs(_temp_folder, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        logging.disable(logging.NOTSET)
        shutil.rmtree(_temp_folder)

    def test_is_image_source_appropriate_returns_true_for_monocular_systems_and_sequential_image_sources(self):
        subject = OrbSlam2(mode=SensorMode.MONOCULAR)
        mock_image_source = mock.create_autospec(ImageSource)

        mock_image_source.sequence_type = ImageSequenceType.SEQUENTIAL
        self.assertTrue(subject.is_image_source_appropriate(mock_image_source))

        mock_image_source.sequence_type = ImageSequenceType.NON_SEQUENTIAL
        self.assertFalse(subject.is_image_source_appropriate(mock_image_source))

        mock_image_source.sequence_type = ImageSequenceType.INTERACTIVE
        self.assertFalse(subject.is_image_source_appropriate(mock_image_source))

    def test_is_image_source_appropriate_returns_true_for_stereo_systems_if_stereo_is_available(self):
        subject = OrbSlam2(mode=SensorMode.STEREO)
        mock_image_source = mock.create_autospec(ImageSource)

        mock_image_source.sequence_type = ImageSequenceType.SEQUENTIAL
        mock_image_source.is_stereo_available = True
        self.assertTrue(subject.is_image_source_appropriate(mock_image_source))

        mock_image_source.is_stereo_available = False
        self.assertFalse(subject.is_image_source_appropriate(mock_image_source))

        mock_image_source.sequence_type = ImageSequenceType.NON_SEQUENTIAL
        mock_image_source.is_stereo_available = True
        self.assertFalse(subject.is_image_source_appropriate(mock_image_source))

        mock_image_source = mock.create_autospec(ImageSource)
        mock_image_source.sequence_type = ImageSequenceType.INTERACTIVE
        self.assertFalse(subject.is_image_source_appropriate(mock_image_source))

    def test_is_image_source_appropriate_returns_true_for_rgbd_systems_if_depth_is_available(self):
        subject = OrbSlam2(mode=SensorMode.RGBD)
        mock_image_source = mock.create_autospec(ImageSource)

        mock_image_source.sequence_type = ImageSequenceType.SEQUENTIAL
        mock_image_source.is_depth_available = True
        self.assertTrue(subject.is_image_source_appropriate(mock_image_source))

        mock_image_source.is_depth_available = False
        self.assertFalse(subject.is_image_source_appropriate(mock_image_source))

        mock_image_source.sequence_type = ImageSequenceType.NON_SEQUENTIAL
        mock_image_source.is_depth_available = True
        self.assertFalse(subject.is_image_source_appropriate(mock_image_source))

        mock_image_source = mock.create_autospec(ImageSource)
        mock_image_source.sequence_type = ImageSequenceType.INTERACTIVE
        self.assertFalse(subject.is_image_source_appropriate(mock_image_source))

    def test_save_settings_raises_error_without_paths_configured(self):
        intrinsics = CameraIntrinsics(
            width=640, height=480, fx=320, fy=320, cx=320, cy=240
        )
        subject = OrbSlam2(mode=SensorMode.MONOCULAR, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(intrinsics, 1 / 30)

        with self.assertRaises(RuntimeError):
            subject.save_settings()

    def test_save_settings_raises_error_without_camera_intrinsics(self):
        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        subject = OrbSlam2(mode=SensorMode.MONOCULAR, vocabulary_file='ORBvoc-tiny.txt')
        subject.resolve_paths(path_manager)

        with self.assertRaises(RuntimeError):
            subject.save_settings()

    def test_save_settings_raises_error_without_camera_baseline_if_stereo(self):
        intrinsics = CameraIntrinsics(width=640, height=480, fx=320, fy=320, cx=320, cy=240)
        path_manager = PathManager([Path(__file__).parent], _temp_folder)

        subject = OrbSlam2(mode=SensorMode.STEREO, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(intrinsics, 1 / 30)
        subject.resolve_paths(path_manager)

        with self.assertRaises(RuntimeError):
            subject.save_settings()

    @mock.patch('arvet_slam.systems.slam.orbslam2.tempfile', autospec=tempfile)
    def test_save_settings_monocular_saves_to_a_temporary_file(self, mock_tempfile):
        mock_tempfile.mkstemp.return_value = (12, 'my_temp_file.yml')
        mock_open = mock.mock_open()
        mock_open.return_value = StringIO()

        intrinsics = CameraIntrinsics(width=640, height=480, fx=320, fy=321, cx=322, cy=240)
        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        subject = OrbSlam2(mode=SensorMode.MONOCULAR, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(intrinsics, 1 / 30)
        subject.resolve_paths(path_manager)

        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock_open, create=True):
            subject.save_settings()

        self.assertTrue(mock_open.called)
        self.assertEqual(Path('my_temp_file.yml'), mock_open.call_args[0][0])

    @mock.patch('arvet_slam.systems.slam.orbslam2.tempfile', autospec=tempfile)
    def test_save_settings_monocular_writes_camera_configuration(self, mock_tempfile):
        mock_tempfile.mkstemp.return_value = (12, 'my_temp_file.yml')
        mock_file = InspectableStringIO()
        mock_open = mock.mock_open()
        mock_open.return_value = mock_file

        intrinsics = CameraIntrinsics(
            width=640, height=480, fx=320, fy=321, cx=322, cy=240,
            k1=1, k2=2, k3=3, p1=4, p2=5
        )
        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        subject = OrbSlam2(mode=SensorMode.MONOCULAR, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(intrinsics, 1 / 29)
        subject.resolve_paths(path_manager)

        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock_open, create=True):
            subject.save_settings()

        contents = mock_file.getvalue()
        lines = contents.split('\n')
        self.assertGreater(len(lines), 0)

        # Camera Configuration
        self.assertIn('Camera.width: 640', lines)
        self.assertIn('Camera.height: 480', lines)
        self.assertIn('Camera.fx: 320.0', lines)
        self.assertIn('Camera.fy: 321.0', lines)
        self.assertIn('Camera.cx: 322.0', lines)
        self.assertIn('Camera.cy: 240.0', lines)
        self.assertIn('Camera.k1: 1.0', lines)
        self.assertIn('Camera.k2: 2.0', lines)
        self.assertIn('Camera.k3: 3.0', lines)
        self.assertIn('Camera.p1: 4.0', lines)
        self.assertIn('Camera.p2: 5.0', lines)
        self.assertIn('Camera.fps: 29.0', lines)
        self.assertIn('Camera.RGB: 1', lines)

    @mock.patch('arvet_slam.systems.slam.orbslam2.tempfile', autospec=tempfile)
    def test_save_settings_stereo_writes_stereo_baseline(self, mock_tempfile):
        mock_tempfile.mkstemp.return_value = (12, 'my_temp_file.yml')
        mock_file = InspectableStringIO()
        mock_open = mock.mock_open()
        mock_open.return_value = mock_file

        intrinsics = CameraIntrinsics(
            width=640, height=480, fx=320, fy=321, cx=322, cy=240,
            k1=1, k2=2, k3=3, p1=4, p2=5
        )
        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        subject = OrbSlam2(mode=SensorMode.STEREO, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(intrinsics, 1 / 29)
        subject.set_stereo_offset(Transform([0.012, -0.142, 0.09]))
        subject.resolve_paths(path_manager)

        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock_open, create=True):
            subject.save_settings()

        contents = mock_file.getvalue()
        lines = contents.split('\n')
        self.assertGreater(len(lines), 0)

        # Camera baseline.
        # Should be fx times the right-offset of the camera (-1 * the y component)
        self.assertIn('Camera.bf: {0}'.format(0.142 * 320), lines)

    @mock.patch('arvet_slam.systems.slam.orbslam2.tempfile', autospec=tempfile)
    def test_save_settings_writes_system_configuration(self, mock_tempfile):
        mock_tempfile.mkstemp.return_value = (12, 'my_temp_file.yml')
        mock_file = InspectableStringIO()
        mock_open = mock.mock_open()
        mock_open.return_value = mock_file

        intrinsics = CameraIntrinsics(width=640, height=480, fx=320, fy=321, cx=322, cy=240)
        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        subject = OrbSlam2(
            mode=SensorMode.MONOCULAR,
            vocabulary_file='ORBvoc-tiny.txt',
            depth_threshold=58.2,
            depthmap_factor=1.22,
            orb_num_features=2337,
            orb_scale_factor=1.32,
            orb_num_levels=16,
            orb_ini_threshold_fast=25,
            orb_min_threshold_fast=14
        )
        subject.set_camera_intrinsics(intrinsics, 1 / 29)
        subject.resolve_paths(path_manager)

        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock_open, create=True):
            subject.save_settings()

        contents = mock_file.getvalue()
        lines = contents.split('\n')
        self.assertGreater(len(lines), 0)

        # Camera Configuration
        self.assertIn('ThDepth: 58.2', lines)
        self.assertIn('DepthMapFactor: 1.22', lines)
        self.assertIn('ORBextractor.nFeatures: 2337', lines)
        self.assertIn('ORBextractor.scaleFactor: 1.32', lines)
        self.assertIn('ORBextractor.nLevels: 16', lines)
        self.assertIn('ORBextractor.iniThFAST: 25', lines)
        self.assertIn('ORBextractor.minThFAST: 14', lines)

    @mock.patch('arvet_slam.systems.slam.orbslam2.tempfile', autospec=tempfile)
    @mock.patch('arvet_slam.systems.slam.orbslam2.multiprocessing', autospec=multiprocessing)
    def test_start_trial_saves_settings_file(self, _, mock_tempfile):
        width = np.random.randint(300, 800)
        height = np.random.randint(300, 800)
        fx = np.random.uniform(0.9, 1.1) * width
        fy = np.random.uniform(0.9, 1.1) * height
        cx = np.random.uniform(0, 1) * width
        cy = np.random.uniform(0, 1) * height
        k1 = np.random.uniform(0, 1)
        k2 = np.random.uniform(0, 1)
        k3 = np.random.uniform(0, 1)
        p1 = np.random.uniform(0, 1)
        p2 = np.random.uniform(0, 1)
        framerate = float(np.random.randint(200, 600) / 10)
        stereo_offset = Transform(np.random.uniform(-1, 1, size=3))

        mock_tempfile.mkstemp.return_value = (12, 'my_temp_file.yml')
        mock_file = InspectableStringIO()
        mock_open = mock.mock_open()
        mock_open.return_value = mock_file

        intrinsics = CameraIntrinsics(
            width=width, height=height, fx=fx, fy=fy, cx=cx, cy=cy, k1=k1, k2=k2, k3=k3, p1=p1, p2=p2
        )
        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        subject = OrbSlam2(
            mode=SensorMode.STEREO,
            vocabulary_file='ORBvoc-tiny.txt',
            depth_threshold=np.random.uniform(0, 255),
            depthmap_factor=np.random.uniform(0, 3),
            orb_num_features=np.random.randint(0, 8000),
            orb_scale_factor=np.random.uniform(0, 2),
            orb_num_levels=np.random.randint(1, 10),
            orb_ini_threshold_fast=np.random.randint(15, 100),
            orb_min_threshold_fast=np.random.randint(0, 15)
        )
        subject.resolve_paths(path_manager)
        subject.set_camera_intrinsics(intrinsics, 1 / framerate)
        subject.set_stereo_offset(stereo_offset)
        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock_open, create=True):
            subject.start_trial(ImageSequenceType.SEQUENTIAL)

        contents = mock_file.getvalue()
        lines = contents.split('\n')
        self.assertGreater(len(lines), 0)

        self.assertEqual('%YAML:1.0', lines[0])
        self.assertIn('Camera.fx: {0}'.format(fx), lines)
        self.assertIn('Camera.fy: {0}'.format(fy), lines)
        self.assertIn('Camera.cx: {0}'.format(cx), lines)
        self.assertIn('Camera.cy: {0}'.format(cy), lines)
        self.assertIn('Camera.k1: {0}'.format(k1), lines)
        self.assertIn('Camera.k2: {0}'.format(k2), lines)
        self.assertIn('Camera.k3: {0}'.format(k3), lines)
        self.assertIn('Camera.p1: {0}'.format(p1), lines)
        self.assertIn('Camera.p2: {0}'.format(p2), lines)
        self.assertIn('Camera.width: {0}'.format(width), lines)
        self.assertIn('Camera.height: {0}'.format(height), lines)
        self.assertIn('Camera.fps: {0}'.format(framerate), lines)
        self.assertIn('Camera.bf: {0}'.format(-1 * stereo_offset.location[1] * fx), lines)
        self.assertIn('ThDepth: {0}'.format(subject.depth_threshold), lines)
        self.assertIn('DepthMapFactor: {0}'.format(subject.depthmap_factor), lines)
        self.assertIn('ORBextractor.nFeatures: {0}'.format(subject.orb_num_features), lines)
        self.assertIn('ORBextractor.scaleFactor: {0}'.format(subject.orb_scale_factor), lines)
        self.assertIn('ORBextractor.nLevels: {0}'.format(subject.orb_num_levels), lines)
        self.assertIn('ORBextractor.iniThFAST: {0}'.format(subject.orb_ini_threshold_fast), lines)
        self.assertIn('ORBextractor.minThFAST: {0}'.format(subject.orb_min_threshold_fast), lines)

    @mock.patch('arvet_slam.systems.slam.orbslam2.multiprocessing', autospec=multiprocessing)
    def test_start_trial_uses_id_in_settings_file_to_avoid_collisions(self, _):
        sys_id = ObjectId()
        mock_open = mock.mock_open()

        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        subject = OrbSlam2(_id=sys_id, mode=SensorMode.MONOCULAR, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(CameraIntrinsics(width=640, height=480, fx=320, fy=321, cx=322, cy=240), 1 / 29)
        subject.resolve_paths(path_manager)

        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock_open, create=True):
            subject.start_trial(ImageSequenceType.SEQUENTIAL)
        self.assertTrue(mock_open.called)
        self.assertIn(str(sys_id), str(mock_open.call_args[0][0]))

    @mock.patch('arvet_slam.systems.slam.orbslam2.multiprocessing', autospec=multiprocessing)
    def test_start_trial_finds_available_file(self, _):
        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        subject = OrbSlam2(mode=SensorMode.MONOCULAR, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(CameraIntrinsics(width=640, height=480, fx=320, fy=321, cx=322, cy=240), 1 / 29)
        subject.resolve_paths(path_manager)

        self.assertIsNone(subject._settings_file)
        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock.mock_open(), create=True):
            subject.start_trial(ImageSequenceType.SEQUENTIAL)
        self.assertIsNotNone(subject._settings_file)
        self.assertTrue(os.path.isfile(subject._settings_file))

    @mock.patch('arvet_slam.systems.slam.orbslam2.multiprocessing', autospec=multiprocessing)
    def test_start_trial_does_nothing_for_non_sequential_input(self, mock_multiprocessing):
        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        mock_process = mock.create_autospec(multiprocessing.Process)
        mock_multiprocessing.Process.return_value = mock_process
        mock_open = mock.mock_open()
        subject = OrbSlam2(mode=SensorMode.MONOCULAR, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(CameraIntrinsics(width=640, height=480, fx=320, fy=321, cx=322, cy=240), 1 / 29)
        subject.resolve_paths(path_manager)
        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock_open, create=True):
            subject.start_trial(ImageSequenceType.NON_SEQUENTIAL)
        self.assertFalse(mock_multiprocessing.Process.called)
        self.assertFalse(mock_process.start.called)
        self.assertFalse(mock_open.called)

    @mock.patch('arvet_slam.systems.slam.orbslam2.multiprocessing', autospec=multiprocessing)
    def test_start_trial_starts_a_subprocess(self, mock_multiprocessing):
        mock_process = mock.create_autospec(multiprocessing.Process)
        mock_multiprocessing.Process.return_value = mock_process

        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        subject = OrbSlam2(mode=SensorMode.MONOCULAR, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(CameraIntrinsics(width=640, height=480, fx=320, fy=321, cx=322, cy=240), 1 / 29)
        subject.resolve_paths(path_manager)

        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock.mock_open(), create=True):
            subject.start_trial(ImageSequenceType.SEQUENTIAL)
        self.assertTrue(mock_multiprocessing.Process.called)
        self.assertEqual(run_orbslam, mock_multiprocessing.Process.call_args[1]['target'])
        self.assertTrue(mock_process.start.called)

    @mock.patch('arvet_slam.systems.slam.orbslam2.multiprocessing', autospec=multiprocessing)
    def test_start_trial_waits_for_a_response_from_a_subprocess(self, mock_multiprocessing):
        mock_queue = mock.create_autospec(multiprocessing.queues.Queue)
        mock_queue.get.return_value = 'ORBSLAM Ready!'
        mock_multiprocessing.Queue.return_value = mock_queue

        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        subject = OrbSlam2(mode=SensorMode.MONOCULAR, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(CameraIntrinsics(width=640, height=480, fx=320, fy=321, cx=322, cy=240), 1 / 29)
        subject.resolve_paths(path_manager)

        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock.mock_open(), create=True):
            subject.start_trial(ImageSequenceType.SEQUENTIAL)
        self.assertTrue(mock_queue.get.called)

    @mock.patch('arvet_slam.systems.slam.orbslam2.multiprocessing', autospec=multiprocessing)
    def test_start_trial_kills_subprocess_and_raises_exception_if_it_gets_no_response(self, mock_multiprocessing):
        mock_process = mock.create_autospec(multiprocessing.Process)
        mock_multiprocessing.Process.return_value = mock_process

        mock_queue = mock.create_autospec(multiprocessing.queues.Queue)
        mock_queue.get.side_effect = QueueEmpty
        mock_multiprocessing.Queue.return_value = mock_queue

        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        subject = OrbSlam2(mode=SensorMode.MONOCULAR, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(CameraIntrinsics(width=640, height=480, fx=320, fy=321, cx=322, cy=240), 1 / 29)
        subject.resolve_paths(path_manager)

        subject.save_settings()
        settings_path = subject._settings_file

        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock.mock_open(), create=True):
            with self.assertRaises(RuntimeError):
                subject.start_trial(ImageSequenceType.SEQUENTIAL)
        self.assertTrue(mock_process.start.called)
        self.assertTrue(mock_process.terminate.called)
        self.assertTrue(mock_process.join.called)
        self.assertTrue(mock_process.kill.called)
        self.assertFalse(settings_path.exists())

    @mock.patch('arvet_slam.systems.slam.orbslam2.multiprocessing', autospec=multiprocessing)
    def test_process_image_mono_sends_image_to_subprocess(self, mock_multiprocessing):
        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        mock_queue = mock.create_autospec(multiprocessing.queues.Queue)     # Have to be specific to get the class
        mock_queue.qsize.return_value = 0
        mock_multiprocessing.Queue.return_value = mock_queue
        image = make_image(SensorMode.MONOCULAR)

        subject = OrbSlam2(mode=SensorMode.MONOCULAR, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(CameraIntrinsics(width=640, height=480, fx=320, fy=321, cx=322, cy=240), 1 / 29)
        subject.resolve_paths(path_manager)

        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock.mock_open(), create=True):
            subject.start_trial(ImageSequenceType.SEQUENTIAL)
        self.assertTrue(mock_multiprocessing.Process.called)
        self.assertIn(mock_queue, mock_multiprocessing.Process.call_args[1]['args'])

        subject.process_image(image, 12)
        self.assertTrue(mock_queue.put.called)
        self.assertIn(12, [elem for elem in mock_queue.put.call_args[0][0] if isinstance(elem, int)])
        self.assertTrue(any(np.array_equal(image.pixels, elem) for elem in mock_queue.put.call_args[0][0]))

    @mock.patch('arvet_slam.systems.slam.orbslam2.multiprocessing', autospec=multiprocessing)
    def test_process_image_rgbd_sends_image_and_depth_to_subprocess(self, mock_multiprocessing):
        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        mock_queue = mock.create_autospec(multiprocessing.queues.Queue)     # Have to be specific to get the class
        mock_queue.qsize.return_value = 0
        mock_multiprocessing.Queue.return_value = mock_queue
        image = make_image(SensorMode.RGBD)

        subject = OrbSlam2(mode=SensorMode.RGBD, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(CameraIntrinsics(width=640, height=480, fx=320, fy=321, cx=322, cy=240), 1 / 29)
        subject.resolve_paths(path_manager)

        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock.mock_open(), create=True):
            subject.start_trial(ImageSequenceType.SEQUENTIAL)
        self.assertTrue(mock_multiprocessing.Process.called)
        self.assertIn(mock_queue, mock_multiprocessing.Process.call_args[1]['args'])

        subject.process_image(image, 12)
        self.assertTrue(mock_queue.put.called)
        self.assertIn(12, [elem for elem in mock_queue.put.call_args[0][0] if isinstance(elem, int)])
        self.assertTrue(any(np.array_equal(image.pixels, elem) for elem in mock_queue.put.call_args[0][0]))
        self.assertTrue(any(np.array_equal(image.depth, elem) for elem in mock_queue.put.call_args[0][0]))

    @mock.patch('arvet_slam.systems.slam.orbslam2.multiprocessing', autospec=multiprocessing)
    def test_process_image_stereo_sends_left_and_right_image_to_subprocess(self, mock_multiprocessing):
        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        mock_queue = mock.create_autospec(multiprocessing.queues.Queue)     # Have to be specific to get the class
        mock_queue.qsize.return_value = 0
        mock_multiprocessing.Queue.return_value = mock_queue
        image = make_image(SensorMode.STEREO)

        subject = OrbSlam2(mode=SensorMode.STEREO, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(CameraIntrinsics(width=640, height=480, fx=320, fy=321, cx=322, cy=240), 1 / 29)
        subject.resolve_paths(path_manager)
        subject.set_stereo_offset(Transform(location=(0.2, -0.6, 0.01)))

        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock.mock_open(), create=True):
            subject.start_trial(ImageSequenceType.SEQUENTIAL)
        self.assertTrue(mock_multiprocessing.Process.called)
        self.assertIn(mock_queue, mock_multiprocessing.Process.call_args[1]['args'])

        subject.process_image(image, 12)
        self.assertTrue(mock_queue.put.called)
        self.assertIn(12, [elem for elem in mock_queue.put.call_args[0][0] if isinstance(elem, int)])
        self.assertTrue(np.any([np.array_equal(image.left_pixels, elem) for elem in mock_queue.put.call_args[0][0]]))
        self.assertTrue(np.any([np.array_equal(image.right_pixels, elem) for elem in mock_queue.put.call_args[0][0]]))

    def test_finish_trial_raises_exception_if_unstarted(self):
        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        subject = OrbSlam2(mode=SensorMode.MONOCULAR, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(CameraIntrinsics(width=640, height=480, fx=320, fy=321, cx=322, cy=240), 1 / 29)
        subject.resolve_paths(path_manager)
        with self.assertRaises(RuntimeError):
            subject.finish_trial()

    @mock.patch('arvet_slam.systems.slam.orbslam2.multiprocessing', autospec=multiprocessing)
    def test_finish_trial_joins_subprocess(self, mock_multiprocessing):
        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        mock_process = mock.create_autospec(multiprocessing.Process)
        mock_multiprocessing.Process.return_value = mock_process
        mock_queue = mock.create_autospec(multiprocessing.queues.Queue)
        mock_queue.qsize.return_value = 0
        mock_queue.get.side_effect = [
            'ORBSLAM Ready!',
            {
                1.3 * idx: [
                    0.122, 15, 6, TrackingState.OK,
                    [
                        1, 0, 0, idx,
                        0, 1, 0, -0.1 * idx,
                        0, 0, 1, 0.22 * (14 - idx)
                    ]
                ]
                for idx in range(10)
            }
        ]
        mock_multiprocessing.Queue.return_value = mock_queue

        subject = OrbSlam2(mode=SensorMode.MONOCULAR, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(CameraIntrinsics(width=640, height=480, fx=320, fy=321, cx=322, cy=240), 1 / 29)
        subject.resolve_paths(path_manager)

        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock.mock_open(), create=True):
            subject.start_trial(ImageSequenceType.SEQUENTIAL)

        subject.finish_trial()
        self.assertTrue(mock_queue.put.called)
        self.assertIsNone(mock_queue.put.call_args[0][0])
        self.assertTrue(mock_process.join.called)

    @mock.patch('arvet_slam.systems.slam.orbslam2.multiprocessing', autospec=multiprocessing)
    def test_finish_trial_returns_result_with_data_from_subprocess(self, mock_multiprocessing):
        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        mock_queue = mock.create_autospec(multiprocessing.queues.Queue)
        mock_queue.qsize.return_value = 0
        mock_queue.get.side_effect = [
            'ORBSLAM Ready!',
            {
                1.3 * idx: [
                    0.122 + 0.09 * idx,     # Processing Time
                    15 + idx,               # Number of features
                    6 + idx,                # Number of matches
                    TrackingState.OK,       # Tracking state
                    [   # Estimated pose
                        1, 0, 0, idx,
                        0, 1, 0, -0.1 * idx,
                        0, 0, 1, 0.22 * (14 - idx)
                    ]
                ]
                for idx in range(10)
            }
        ]
        mock_multiprocessing.Queue.return_value = mock_queue
        image_ids = []

        subject = OrbSlam2(mode=SensorMode.MONOCULAR, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(CameraIntrinsics(width=640, height=480, fx=320, fy=321, cx=322, cy=240), 1 / 29)
        subject.resolve_paths(path_manager)

        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock.mock_open(), create=True):
            subject.start_trial(ImageSequenceType.SEQUENTIAL)
        for idx in range(10):
            image = make_image(SensorMode.MONOCULAR)
            image.metadata.camera_pose = Transform((0.25 * (14 - idx), -1.1 * idx, 0.11 * idx))
            image_ids.append(image.pk)
            subject.process_image(image, 1.3 * idx)
        trial_result = subject.finish_trial()
        self.assertIsInstance(trial_result, SLAMTrialResult)
        self.assertTrue(trial_result.success)
        self.assertGreater(trial_result.run_time, 0)
        self.assertIsNotNone(trial_result.settings)
        self.assertEqual(10, len(trial_result.results))
        for idx in range(10):
            frame_result = trial_result.results[idx]
            with no_auto_dereference(FrameResult):
                self.assertEqual(image_ids[idx], frame_result.image)
            self.assertEqual(Transform((0.25 * (14 - idx), -1.1 * idx, 0.11 * idx)), frame_result.pose)
            self.assertEqual(1.3 * idx, frame_result.timestamp)
            self.assertEqual(15 + idx, frame_result.num_features)
            self.assertEqual(6 + idx, frame_result.num_matches)
            # Coordinates of the estimated pose should be rearranged
            self.assertEqual(Transform([0.22 * (14 - idx), -1 * idx, 0.1 * idx]), frame_result.estimated_pose)

    @mock.patch('arvet_slam.systems.slam.orbslam2.logging', autospec=logging)
    @mock.patch('arvet_slam.systems.slam.orbslam2.multiprocessing', autospec=multiprocessing)
    def test_logs_timestamps_returned_by_subprocess_without_matching_frame(self, mock_multiprocessing, mock_logging):
        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        mock_queue = mock.create_autospec(multiprocessing.queues.Queue)
        mock_queue.qsize.return_value = 0
        mock_queue.get.side_effect = [
            'ORBSLAM Ready!',
            {
                1.3 * idx: [
                    0.122 + 0.09 * idx,     # Processing Time
                    15 + idx,               # Number of features
                    6 + idx,                # Number of matches
                    TrackingState.OK,       # Tracking state
                    [  # Estimated pose
                        1, 0, 0, idx,
                        0, 1, 0, -0.1 * idx,
                        0, 0, 1, 0.22 * (14 - idx)
                    ]
                ]
                for idx in range(10)
            }
        ]
        mock_multiprocessing.Queue.return_value = mock_queue

        mock_logger = mock.MagicMock()
        mock_logging.getLogger.return_value = mock_logger

        subject = OrbSlam2(mode=SensorMode.MONOCULAR, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(CameraIntrinsics(width=640, height=480, fx=320, fy=321, cx=322, cy=240), 1 / 29)
        subject.resolve_paths(path_manager)

        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock.mock_open(), create=True):
            subject.start_trial(ImageSequenceType.SEQUENTIAL)
        # Finish without giving it any frames
        trial_result = subject.finish_trial()

        self.assertIsInstance(trial_result, SLAMTrialResult)
        self.assertFalse(trial_result.success)
        self.assertEqual(0, len(trial_result.results))
        self.assertTrue(mock_logger.warning.called)
        for idx in range(10):
            # Look for the missing timestamps in the log messages
            self.assertTrue(any(str(1.3 * idx) in call_args[0][0] for call_args in mock_logger.warning.call_args_list))

    @mock.patch('arvet_slam.systems.slam.orbslam2.multiprocessing', autospec=multiprocessing)
    def test_finish_trial_cleans_up_and_raises_exception_if_cannot_get_data_from_subprocess(self, mock_multiprocessing):
        path_manager = PathManager([Path(__file__).parent], _temp_folder)
        mock_process = mock.create_autospec(multiprocessing.Process)
        mock_multiprocessing.Process.return_value = mock_process
        mock_queue = mock.create_autospec(multiprocessing.queues.Queue)
        mock_queue.qsize.return_value = 0
        mock_queue.get.side_effect = ['ORBSLAM ready!', QueueEmpty()]
        mock_multiprocessing.Queue.return_value = mock_queue

        subject = OrbSlam2(mode=SensorMode.MONOCULAR, vocabulary_file='ORBvoc-tiny.txt')
        subject.set_camera_intrinsics(CameraIntrinsics(width=640, height=480, fx=320, fy=321, cx=322, cy=240), 1 / 29)
        subject.resolve_paths(path_manager)

        subject.save_settings()
        settings_file = subject._settings_file

        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock.mock_open(), create=True):
            subject.start_trial(ImageSequenceType.SEQUENTIAL)

        with self.assertRaises(RuntimeError):
            subject.finish_trial()
        self.assertTrue(mock_process.join.called)
        self.assertFalse(settings_file.exists())


class TestDumpConfig(unittest.TestCase):
    config = {
        'Camera': {
            'fx': 320,
            'fy': 240,
            'cx': 320,
            'cy': 240,
            'k1': 0,
            'k2': 0,
            'p1': 0,
            'p2': 0,
            'k3': 0,
            'width': 640,
            'height': 480,
            'fps': 30.0,
            'RGB': 1
        },
        'ThDepth': 70,
        'DepthMapFactor': 1.2,
        'ORBextractor': {
            'nFeatures': 2000,
            'scaleFactor': 1.2,
            'nLevels': 8,
            'iniThFAST': 12,
            'minThFAST': 7
        },
        'Viewer': {
            'KeyFrameSize': 0.05,
            'KeyFrameLineWidth': 1,
            'GraphLineWidth': 0.9,
            'PointSize': 2,
            'CameraSize': 0.08,
            'CameraLineWidth': 3,
            'ViewpointX': 0,
            'ViewpointY': -0.7,
            'ViewpointZ': -1.8,
            'ViewpointF': 500
        }
    }

    def test_opens_and_writes_to_specified_file(self):
        mock_file = InspectableStringIO()
        mock_open = mock.mock_open()
        mock_open.return_value = mock_file

        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock_open, create=True):
            dump_config('test_conf.yml', self.config)
        self.assertTrue(mock_open.called)
        self.assertEqual('test_conf.yml', mock_open.call_args[0][0])
        self.assertGreater(len(mock_file.getvalue()), 0)

    def test_dump_config_writes_yaml_header(self):
        mock_file = InspectableStringIO()
        mock_open = mock.mock_open()
        mock_open.return_value = mock_file

        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock_open, create=True):
            dump_config('test_conf.yml', self.config)

        contents = mock_file.getvalue()
        lines = contents.split('\n')
        self.assertGreater(len(lines), 0)
        self.assertEqual('%YAML:1.0', lines[0])

    def test_nested_to_dotted_converts_arbitrary_dicts(self):
        key_chars = list(string.ascii_letters + string.digits)
        expected_key = 'foobar'
        value = 12
        nested_dict = {expected_key: value}
        self.assertEqual(nested_to_dotted(nested_dict), {expected_key: value})
        for _ in range(10):
            new_key = ''.join(np.random.choice(key_chars) for _ in range(10))
            nested_dict = {new_key: nested_dict}
            expected_key = new_key + '.' + expected_key
            self.assertEqual(nested_to_dotted(nested_dict), {expected_key: value})

    def test_converts_nested_keys_to_dots(self):
        mock_file = InspectableStringIO()
        mock_open = mock.mock_open()
        mock_open.return_value = mock_file

        with mock.patch('arvet_slam.systems.slam.orbslam2.open', mock_open, create=True):
            dump_config('test_conf.yml', self.config)

        contents = mock_file.getvalue()
        lines = contents.split('\n')

        expected_conf = nested_to_dotted(self.config)
        for key, value in expected_conf.items():
            self.assertIn('{key}: {value}'.format(key=key, value=value), lines)


class TestMakeRelativePose(ExtendedTestCase):

    def test_returns_transform_object(self):
        frame_delta = np.array([[1, 0, 0, 10],
                                [0, 1, 0, -22.4],
                                [0, 0, 1, 13.2],
                                [0, 0, 0, 1]])
        pose = make_relative_pose(frame_delta)
        self.assertIsInstance(pose, Transform)

    def test_rearranges_location_coordinates(self):
        frame_delta = np.array([[1, 0, 0, 10],
                                [0, 1, 0, -22.4],
                                [0, 0, 1, 13.2],
                                [0, 0, 0, 1]])
        pose = make_relative_pose(frame_delta)
        self.assertNPEqual((13.2, -10, 22.4), pose.location)

    def test_changes_rotation_each_axis(self):
        frame_delta = np.array([[1, 0, 0, 10],
                                [0, 1, 0, -22.4],
                                [0, 0, 1, 13.2],
                                [0, 0, 0, 1]])
        # Roll, rotation around z-axis for libviso2
        frame_delta[0:3, 0:3] = tf3d.axangles.axangle2mat((0, 0, 1), np.pi / 6, True)
        pose = make_relative_pose(frame_delta)
        self.assertNPClose((np.pi / 6, 0, 0), pose.euler)

        # Pitch, rotation around x-axis for libviso2
        frame_delta[0:3, 0:3] = tf3d.axangles.axangle2mat((1, 0, 0), np.pi / 6, True)
        pose = make_relative_pose(frame_delta)
        self.assertNPClose((0, -np.pi / 6, 0), pose.euler)

        # Yaw, rotation around negative y-axis for libviso2
        frame_delta[0:3, 0:3] = tf3d.axangles.axangle2mat((0, 1, 0), np.pi / 6, True)
        pose = make_relative_pose(frame_delta)
        self.assertNPClose((0, 0, -np.pi / 6), pose.euler)

    def test_combined(self):
        frame_delta = np.identity(4)
        for _ in range(10):
            loc = np.random.uniform(-1000, 1000, 3)
            rot_axis = np.random.uniform(-1, 1, 3)
            rot_angle = np.random.uniform(-np.pi, np.pi)
            frame_delta[0:3, 3] = -loc[1], -loc[2], loc[0]
            frame_delta[0:3, 0:3] = tf3d.axangles.axangle2mat((-rot_axis[1], -rot_axis[2], rot_axis[0]),
                                                              rot_angle, False)
            pose = make_relative_pose(frame_delta)
            self.assertNPEqual(loc, pose.location)
            self.assertNPClose(tf3d.quaternions.axangle2quat(rot_axis, rot_angle, False), pose.rotation_quat(True))


class InspectableStringIO(StringIO):
    """
    A tiny modification on StringIO to preserve the value
    This can be returned from a mocked open() call to act like a file,
    and then allow inspection of the file contents.
    Does not have mos
    """

    def __init__(self):
        super(InspectableStringIO, self).__init__()
        self.final_value = ''

    def close(self) -> None:
        self.final_value = self.getvalue()
        super(InspectableStringIO, self).close()

    def getvalue(self) -> str:
        if self.closed:
            return self.final_value
        return super(InspectableStringIO, self).getvalue()


def make_image(img_type: SensorMode):
    pixels = np.random.randint(0, 255, (32, 32, 3), dtype='uint8')
    depth = None
    if img_type == SensorMode.RGBD:
        depth = np.random.normal(1.0, 0.01, size=(32, 32)).astype(np.float16)
    metadata = imeta.make_metadata(
        pixels=pixels,
        depth=depth,
        source_type=imeta.ImageSourceType.SYNTHETIC,
        camera_pose=Transform(location=[13.8, 2.3, -9.8]),
        intrinsics=CameraIntrinsics(
            width=pixels.shape[1],
            height=pixels.shape[0],
            fx=16, fy=16, cx=16, cy=16
        )
    )
    if img_type == SensorMode.STEREO:
        right_pixels = np.random.randint(0, 255, (32, 32, 3), dtype='uint8')
        right_metadata = imeta.make_right_metadata(right_pixels, metadata)
        return StereoImage(
            _id=ObjectId(),
            pixels=pixels,
            metadata=metadata,
            right_pixels=right_pixels,
            right_metadata=right_metadata
        )
    elif img_type == SensorMode.RGBD:
        return Image(
            _id=ObjectId(),
            pixels=pixels,
            depth=depth,
            metadata=metadata
        )
    return Image(
        _id=ObjectId(),
        pixels=pixels,
        metadata=metadata
    )
