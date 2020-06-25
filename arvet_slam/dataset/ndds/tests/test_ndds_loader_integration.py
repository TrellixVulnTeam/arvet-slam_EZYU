import unittest
import os.path
import logging
import shutil
from json import load as json_load
from pathlib import Path
import arvet.database.tests.database_connection as dbconn
import arvet.database.image_manager as im_manager
from arvet.core.image_collection import ImageCollection
from arvet.core.image import StereoImage
import arvet_slam.dataset.ndds.ndds_loader as ndds_loader
from arvet_slam.dataset.ndds.depth_noise import DepthNoiseQuality


def load_dataset_location():
    conf_json = Path(__file__).parent / 'ndds_location.json'
    if conf_json.is_file():
        with conf_json.open('r') as fp:
            son = json_load(fp)
            return Path(son['location']), str(son['sequence']), str(son['zip_sequence'])
    return None, None, None


dataset_root, sequence, zipped_sequence = load_dataset_location()


class TestNDDSLoaderIntegration(unittest.TestCase):

    @unittest.skipIf(
        dataset_root is None or not (dataset_root / sequence).exists(),
        f"Could not find the NDDS dataset at {dataset_root / sequence}, cannot run integration test"
    )
    def test_load_configured_sequence(self):
        dbconn.connect_to_test_db()
        image_manager = im_manager.DefaultImageManager(dbconn.image_file, allow_write=True)
        im_manager.set_image_manager(image_manager)
        logging.disable(logging.CRITICAL)

        # count the number of images we expect to import
        left_folder = dataset_root / sequence / 'left'
        right_folder = dataset_root / sequence / 'right'
        num_images = sum(
            1 for file in left_folder.iterdir()
            if file.is_file()
            and file.suffix == '.png'
            and '.' not in file.stem
            and (right_folder / file.name).exists()
        )

        # Make sure there is nothing in the database
        ImageCollection.objects.all().delete()
        StereoImage.objects.all().delete()

        result = ndds_loader.import_dataset(
            dataset_root / sequence,
            DepthNoiseQuality.KINECT_NOISE.name
        )
        self.assertIsInstance(result, ImageCollection)
        self.assertIsNotNone(result.pk)
        self.assertTrue(result.is_depth_available)
        self.assertTrue(result.is_stereo_available)

        self.assertEqual(1, ImageCollection.objects.all().count())
        self.assertEqual(num_images, StereoImage.objects.all().count())   # Make sure we got all the images

        # Make sure we got the depth and position data
        for timestamp, image in result:
            self.assertIsNotNone(image.camera_pose)
            self.assertIsNotNone(image.right_camera_pose)

        # Clean up after ourselves by dropping the collections for the models
        ImageCollection._mongometa.collection.drop()
        StereoImage._mongometa.collection.drop()
        if os.path.isfile(dbconn.image_file):
            os.remove(dbconn.image_file)
        logging.disable(logging.NOTSET)
