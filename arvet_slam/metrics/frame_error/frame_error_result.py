# Copyright (c) 2018, John Skinner
import typing
from enum import Enum
from itertools import chain
from operator import attrgetter
import bson
import numpy as np

import pymodm
import pymodm.fields as fields
from pymodm.queryset import QuerySet
from pymodm.manager import Manager
from pymodm.context_managers import no_auto_dereference

from arvet.database.reference_list_field import ReferenceListField
from arvet.database.enum_field import EnumField
from arvet.database.transform_field import TransformField
from arvet.core.system import VisionSystem
from arvet.core.image_source import ImageSource
from arvet.core.image import Image
from arvet.core.trial_result import TrialResult
from arvet.core.metric import Metric, MetricResult
from arvet.util.column_list import ColumnList
import arvet.util.transform as tf
from arvet_slam.trials.slam.visual_slam import FrameResult
from arvet_slam.trials.slam.tracking_state import TrackingState


class PoseError(pymodm.EmbeddedMongoModel):
    """
    Errors in a pose estimate. Given two poses, these are the ways we measure the difference between them.
    We have 5 different numbers for the translation error:
    x, y, z (cartesian coordinates), length & direction (polar coordinates)
    The orientation error has a single value: "rot"
    """
    x = fields.FloatField(required=True)
    y = fields.FloatField(required=True)
    z = fields.FloatField(required=True)
    length = fields.FloatField(required=True)
    direction = fields.FloatField(required=True)
    rot = fields.FloatField(required=True)


def make_pose_error(estimated_pose: tf.Transform, reference_pose: tf.Transform) -> PoseError:
    """
    Make a pose error object from an estimated
    :param estimated_pose:
    :param reference_pose:
    :return:
    """
    trans_error = estimated_pose.location - reference_pose.location
    trans_error_length = np.linalg.norm(trans_error)

    trans_error_direction = np.nan  # No direction if the vectors are the same
    if trans_error_length > 0:
        # Get the unit vector in the direction of the true location
        reference_norm = np.linalg.norm(reference_pose.location)
        if reference_norm > 0:
            unit_reference = reference_pose.location / reference_norm
            # Find the angle between the trans error and the true location
            dot_product = np.dot(trans_error / trans_error_length, unit_reference)
            trans_error_direction = np.arccos(
                # Clip to arccos range to avoid errors
                min(1.0, max(0.0, dot_product))
            )
    # Different to the trans_direction, this is the angle between the estimated orientation and true orientation
    rot_error = tf.quat_diff(estimated_pose.rotation_quat(w_first=True), reference_pose.rotation_quat(w_first=True))
    return PoseError(
        x=trans_error[0],
        y=trans_error[1],
        z=trans_error[2],
        length=trans_error_length,
        direction=trans_error_direction,
        rot=rot_error
    )


class FrameError(pymodm.MongoModel):
    """
    All the errors from a single frame
    One of these gets created for each frame for each trial
    """
    trial_result = fields.ReferenceField(TrialResult, required=True)
    image = fields.ReferenceField(Image, required=True, on_delete=fields.ReferenceField.CASCADE)

    repeat = fields.IntegerField(required=True)
    timestamp = fields.FloatField(required=True)
    motion = TransformField(required=True)
    processing_time = fields.FloatField(default=np.nan)
    num_features = fields.IntegerField(default=0)
    num_matches = fields.IntegerField(default=0)

    tracking = EnumField(TrackingState, default=TrackingState.OK)
    absolute_error = fields.EmbeddedDocumentField(PoseError, blank=True)
    relative_error = fields.EmbeddedDocumentField(PoseError, blank=True)
    noise = fields.EmbeddedDocumentField(PoseError, blank=True)

    system_properties = fields.DictField(blank=True)
    image_properties = fields.DictField(blank=True)

    columns = ColumnList(
        repeat=attrgetter('repeat'),
        timestamp=attrgetter('timestamp'),
        tracking=attrgetter('is_tracking'),
        processing_time=attrgetter('processing_time'),
        motion_x=attrgetter('motion.x'),
        motion_y=attrgetter('motion.y'),
        motion_z=attrgetter('motion.z'),
        motion_roll=lambda obj: obj.motion.euler[0],
        motion_pitch=lambda obj: obj.motion.euler[1],
        motion_yaw=lambda obj: obj.motion.euler[2],
        num_features=attrgetter('num_features'),
        num_matches=attrgetter('num_matches'),

        abs_error_x=lambda obj: obj.absolute_error.x if obj.absolute_error is not None else np.nan,
        abs_error_y=lambda obj: obj.absolute_error.y if obj.absolute_error is not None else np.nan,
        abs_error_z=lambda obj: obj.absolute_error.z if obj.absolute_error is not None else np.nan,
        abs_error_length=lambda obj: obj.absolute_error.length if obj.absolute_error is not None else np.nan,
        abs_error_direction=lambda obj: obj.absolute_error.direction if obj.absolute_error is not None else np.nan,
        abs_rot_error=lambda obj: obj.absolute_error.rot if obj.absolute_error is not None else np.nan,

        trans_error_x=lambda obj: obj.relative_error.x if obj.relative_error is not None else np.nan,
        trans_error_y=lambda obj: obj.relative_error.y if obj.relative_error is not None else np.nan,
        trans_error_z=lambda obj: obj.relative_error.z if obj.relative_error is not None else np.nan,
        trans_error_length=lambda obj: obj.relative_error.length if obj.relative_error is not None else np.nan,
        trans_error_direction=lambda obj: obj.relative_error.direction if obj.relative_error is not None else np.nan,
        rot_error=lambda obj: obj.relative_error.rot if obj.relative_error is not None else np.nan,

        trans_noise_x=lambda obj: obj.noise.x if obj.noise is not None else np.nan,
        trans_noise_y=lambda obj: obj.noise.y if obj.noise is not None else np.nan,
        trans_noise_z=lambda obj: obj.noise.z if obj.noise is not None else np.nan,
        trans_noise_length=lambda obj: obj.noise.length if obj.noise is not None else np.nan,
        trans_noise_direction=lambda obj: obj.noise.direction if obj.noise is not None else np.nan,
        rot_noise=lambda obj: obj.noise.rot if obj.noise is not None else np.nan,
    )

    @property
    def is_tracking(self) -> bool:
        return self.tracking is TrackingState.OK

    def get_columns(self) -> typing.Set[str]:
        """
        Get the columns available to this frame error result
        :return:
        """
        return set(self.columns.keys()) | set(self.system_properties.keys()) | set(self.image_properties.keys())

    def get_properties(self, columns: typing.Iterable[str] = None, other_properties: dict = None):
        """
        Flatten the frame error to a dictionary.
        This is used to construct rows in a Pandas data frame, so the keys are column names
        Handles pulling data from the linked system and linked image
        :return:
        """
        if other_properties is None:
            other_properties = {}
        if columns is None:
            columns = set(self.columns.keys()) | set(self.system_properties.keys()) | set(self.image_properties.keys())
        error_properties = {
            column_name: self.columns.get_value(self, column_name)
            for column_name in columns
            if column_name in self.columns
        }
        image_properties = {
            column: self.image_properties[column]
            for column in columns
            if column in self.image_properties
        }
        system_properties = {
            column: self.system_properties[column]
            for column in columns
            if column in self.system_properties
        }
        return {
            **other_properties,
            **image_properties,
            **system_properties,
            **error_properties
        }


def make_frame_error(
        trial_result: TrialResult,
        frame_result: FrameResult,
        image: typing.Union[None, Image],
        system: typing.Union[None, VisionSystem],
        repeat_index: int,
        absolute_error: typing.Union[None, PoseError],
        relative_error: typing.Union[None, PoseError],
        noise: typing.Union[None, PoseError]
) -> FrameError:
    """
    Construct a frame_error object from a context
    The frame error copies data from it's linked objects (like the system or image)
    to avoid having to dereference them later.
    This function makes sure that data is consistent.

    It takes the system and image, even though the trial result and frame result should refer to those
    because you're usually creating lots of FrameError objects at the same time, so you should load
    those objects _once_, and pass them in each time.
    Just because you've loaded the object doesn't mean the FrameResult has that object

    :param trial_result: The trial result producing this FrameError
    :param frame_result: The FrameResult for the specific frame this error corresponds to
    :param image: The specific image this error corresponds to. Will be pulled from frame_result if None.
    :param system: The system that produced the trial_result. Will be pulled from the trial result if None.
    :param repeat_index: The repeat index of the trial result, for identification within the set
    :param absolute_error: The error in the estimated pose, in an absolute reference frame.
    :param relative_error: The error in teh estimated motion, relative to the previous frame.
    :param noise: The error between this particular motion estimate, and the average motion estimate from all trials.
    :return: A FrameError object, containing the errors, and related metadata.
    """
    # Make sure the image we're given is the same as the one from the frame_result, without reloading it
    if image is None:
        image = frame_result.image
    else:
        with no_auto_dereference(type(frame_result)):
            if isinstance(frame_result.image, bson.ObjectId):
                image_id = frame_result.image
            else:
                image_id = frame_result.image.pk
        if image_id != image.pk:
            image = frame_result.image

    # Make sure the given system matches the trial result, avoiding loading it unnecessarily
    if system is None:
        system = trial_result.system
    else:
        with no_auto_dereference(type(trial_result)):
            if isinstance(trial_result.system, bson.ObjectId):
                system_id = trial_result.system
            else:
                system_id = trial_result.system.pk
        if system_id != system.pk:
            system = trial_result.system

    # Read the system properties from the trial result
    system_properties = system.get_properties(None, trial_result.settings)
    image_properties = image.get_properties()
    return FrameError(
        trial_result=trial_result,
        image=image,
        repeat=repeat_index,
        timestamp=frame_result.timestamp,
        motion=frame_result.motion,
        processing_time=frame_result.processing_time,
        num_features=frame_result.num_features,
        num_matches=frame_result.num_matches,
        tracking=frame_result.tracking_state,
        absolute_error=absolute_error,
        relative_error=relative_error,
        noise=noise,
        system_properties={str(k): json_value(v) for k, v in system_properties.items()},
        image_properties={str(k): json_value(v) for k, v in image_properties.items()}
    )


class TrialErrors(pymodm.EmbeddedMongoModel):
    frame_errors = ReferenceListField(FrameError, required=True, blank=True)
    frames_lost = fields.ListField(fields.IntegerField(), blank=True)
    frames_found = fields.ListField(fields.IntegerField(), blank=True)
    times_lost = fields.ListField(fields.FloatField(), blank=True)
    times_found = fields.ListField(fields.FloatField(), blank=True)
    distances_lost = fields.ListField(fields.FloatField(), blank=True)
    distances_found = fields.ListField(fields.FloatField(), blank=True)


class FrameErrorResultQuerySet(QuerySet):

    def delete(self):
        """
        When a frame error result is deleted, also delete the frame errors it refers to
        :return:
        """
        frame_error_ids = set(err_id for doc in self.values()
                              for trial_errors in doc['errors'] for err_id in trial_errors['frame_errors'])
        FrameError.objects.raw({'_id': {'$in': list(frame_error_ids)}}).delete()
        super(FrameErrorResultQuerySet, self).delete()


FrameErrorResultManger = Manager.from_queryset(FrameErrorResultQuerySet)


class FrameErrorResult(MetricResult):
    """
    Error observations per estimate of a pose
    """
    system = fields.ReferenceField(VisionSystem, required=True, on_delete=pymodm.ReferenceField.CASCADE)
    image_source = fields.ReferenceField(ImageSource, required=True, on_delete=pymodm.ReferenceField.CASCADE)
    errors = fields.EmbeddedDocumentListField(TrialErrors, required=True, blank=True)
    image_source_properties = fields.DictField(blank=True)
    metric_properties = fields.DictField(blank=True)
    frame_columns = fields.ListField(fields.CharField(), blank=True)

    objects = FrameErrorResultManger()

    def save(self, cascade=None, full_clean=True, force_insert=False):
        """
        When saving, also save the frame results
        :param cascade:
        :param full_clean:
        :param force_insert:
        :return:
        """
        # Cascade the save to the frame errors
        if cascade or (self._mongometa.cascade and cascade is not False):
            frame_errors_to_create = [
                frame_error
                for trial_errors in self.errors
                for frame_error in trial_errors.frame_errors
                if frame_error.pk is None
            ]
            frame_errors_to_save = [
                frame_error
                for trial_errors in self.errors
                for frame_error in trial_errors.frame_errors
                if frame_error.pk is not None
            ]
            # Do error creation in bulk. Updates still happen individually.
            if len(frame_errors_to_create) > 0:
                new_ids = FrameError.objects.bulk_create(frame_errors_to_create, full_clean=full_clean)
                for new_id, model in zip(new_ids, frame_errors_to_create):
                    model.pk = new_id
            for frame_error in frame_errors_to_save:
                frame_error.save(cascade, full_clean, force_insert)
        super(FrameErrorResult, self).save(cascade, full_clean, force_insert)

    def get_columns(self) -> typing.Set[str]:
        """
        Get the available columns from the frame error result
        :return:
        """
        funcs = ['min', 'max', 'mean', 'median', 'std']
        values = ['frames_lost', 'frames_found', 'times_lost', 'times_found', 'distance_lost', 'distance_found']
        columns = (
            set(func + '_' + val for func in funcs for val in values) |
            set(self.image_source_properties.keys()) |
            set(self.metric_properties.keys()) |
            set(self.frame_columns)
        )
        return columns

    def get_results(self, columns: typing.Iterable[str] = None) -> typing.List[dict]:
        """
        Collate together the results for this metric result
        Can capture many different independent variables, including those from the system
        :param columns:
        :return:
        """
        if columns is None:
            # If no columns, do all columns
            columns = self.get_columns()
        columns = set(columns)
        other_properties = {
            column: self.image_source_properties[column]
            for column in self.image_source_properties.keys()
            if column in columns
        }
        other_properties.update({
            column: self.metric_properties[column]
            for column in self.metric_properties.keys()
            if column in columns
        })

        # Find column values that need to be computed for this object
        # Possibilities are any of 5 aggregate functions (min, max, mean, median, std)
        # applied to any of frames_lost, frames_found, time_lost, time_found, distance_lost, or distance_found.
        # These will be evaluated for each separate trial, and aggregated with the results from that trial.
        # we pre-compute to avoid checking which columns are actually specified every repeat
        funcs = [('min', np.min), ('max', np.max), ('mean', np.mean), ('median', np.median), ('std', np.std)]
        values = [('frames_lost', 'frames_lost'), ('frames_found', 'frames_found'),
                  ('times_lost', 'times_lost'), ('times_found', 'times_found'),
                  ('distance_lost', 'distances_lost'), ('distance_found', 'distances_found')]
        columns_to_compute = [
            (func_name + '_' + col_name, func, data)
            for col_name, data in values
            for func_name, func in funcs
            if func_name + '_' + col_name in columns
        ]

        results = []
        for trial_errors in self.errors:
            # Compute the values of certain columns available from this result
            for column, func, attribute in columns_to_compute:
                data = getattr(trial_errors, attribute)
                other_properties[column] = func(data) if len(data) > 0 else np.nan

            results.extend([
                frame_error.get_properties(columns, other_properties)
                for frame_error in trial_errors.frame_errors
            ])
        return results


def make_frame_error_result(
        metric: Metric,
        trial_results: typing.List[TrialResult],
        errors: typing.List[TrialErrors]
) -> FrameErrorResult:
    """
    Construct a frame error
    Pulls some data from the linked trial results, metric, and errors for de-normalisation.

    :param metric: The metric producing this frame error result.
    :param trial_results: The set of trial results being measured. Also used to retrieve the system and image source.
    :param errors: The set of TrialErrors for each trial result
    :return: A FrameErrorResult object ready to save.
    """
    image_source = trial_results[0].image_source
    image_source_properties = image_source.get_properties()
    metric_properties = metric.get_properties()
    # Ask all the frame error objects what columns they have. Expect significant redundancy
    frame_error_columns = list(set(chain.from_iterable(
        frame_error.get_columns()
        for trial_errors in errors
        for frame_error in trial_errors.frame_errors
    )))
    return FrameErrorResult(
        metric=metric,
        trial_results=trial_results,
        system=trial_results[0].system,
        image_source=image_source,
        success=True,
        errors=errors,
        image_source_properties={str(k): json_value(v) for k, v in image_source_properties.items()},
        metric_properties={str(k): json_value(v) for k, v in metric_properties.items()},
        frame_columns=frame_error_columns
    )


def json_value(value):
    """
    Ensure a particular value is json serialisable, and thus can be saved to the database.
    Flattens numpy types to int or float, and enums to their string name.
    :param value:
    :return:
    """
    if isinstance(value, Enum):
        return value.name
    elif np.issubdtype(type(value), np.integer):
        return int(value)
    elif np.issubdtype(type(value), np.floating):
        return float(value)
    return value
