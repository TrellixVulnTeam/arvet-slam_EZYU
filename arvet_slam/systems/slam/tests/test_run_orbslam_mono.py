# Copyright (c) 2017, John Skinner
import unittest
import os.path
import shutil
from pathlib import Path
from pymodm.context_managers import no_auto_dereference

from arvet.config.path_manager import PathManager
from arvet.core.sequence_type import ImageSequenceType
from arvet_slam.trials.slam.tracking_state import TrackingState
from arvet_slam.trials.slam.visual_slam import SLAMTrialResult
from arvet_slam.systems.slam.orbslam2 import OrbSlam2, SensorMode
from arvet_slam.systems.slam.tests.demo_image_builder import DemoImageBuilder
from arvet_slam.systems.slam.tests.create_vocabulary import create_vocab


class TestRunOrbslamMono(unittest.TestCase):
    temp_folder = 'temp-test-orbslam2'
    vocab_path = Path(__file__).parent / 'ORBvoc-synth.txt'

    @classmethod
    def setUpClass(cls):
        os.makedirs(cls.temp_folder, exist_ok=True)
        if not cls.vocab_path.exists():  # If there is no vocab file, make one
            print("Creating vocab file, this may take a while...")
            create_vocab(cls.vocab_path)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.temp_folder)

    def test_simple_trial_run(self):
        # Actually run the system using mocked images
        num_frames = 50
        max_time = 50
        speed = 0.1
        path_manager = PathManager([Path(__file__).parent], self.temp_folder)
        image_builder = DemoImageBuilder(
            mode=SensorMode.MONOCULAR,
            width=160, height=120, num_stars=2000,
            length=max_time * speed, speed=speed,
            close_ratio=0.4, min_size=1, max_size=5
        )
        subject = OrbSlam2(
            vocabulary_file=self.vocab_path,
            mode=SensorMode.MONOCULAR,
            orb_num_levels=1,
            orb_ini_threshold_fast=3,
            orb_min_threshold_fast=3
        )
        subject.resolve_paths(path_manager)
        subject.set_camera_intrinsics(image_builder.get_camera_intrinsics(), max_time / num_frames)

        subject.start_trial(ImageSequenceType.SEQUENTIAL)
        for idx in range(num_frames):
            time = max_time * idx / num_frames
            image = image_builder.create_frame(time)
            subject.process_image(image, time)
        result = subject.finish_trial()

        self.assertIsInstance(result, SLAMTrialResult)
        with no_auto_dereference(SLAMTrialResult):
            self.assertEqual(subject.pk, result.system)
        self.assertTrue(result.success)
        self.assertFalse(result.has_scale)
        self.assertIsNotNone(result.run_time)
        self.assertIsNotNone(result.settings)
        self.assertEqual(num_frames, len(result.results))

        has_been_found = False
        for idx, frame_result in enumerate(result.results):
            self.assertEqual(max_time * idx / num_frames, frame_result.timestamp)
            self.assertIsNotNone(frame_result.pose)
            self.assertIsNotNone(frame_result.motion)
            if frame_result.tracking_state is TrackingState.OK:
                has_been_found = True
        self.assertTrue(has_been_found)
