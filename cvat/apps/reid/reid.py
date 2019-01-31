# Copyright (C) 2018 Intel Corporation
#
# SPDX-License-Identifier: MIT

import os
import rq
import cv2
import math
import numpy
import fnmatch

from openvino.inference_engine import IENetwork, IEPlugin
from scipy.optimize import linear_sum_assignment
from scipy.spatial.distance import euclidean, cosine

from cvat.apps.engine.models import Job


class ReID:
    __treshold = None
    __max_distance = None
    __frame_urls = None
    __frame_boxes = None
    __stop_frame = None
    __plugin = None
    __executable_network = None
    __input_blob_name = None
    __output_blob_name = None
    __input_height = None
    __input_width = None


    def __init__(self, jid, data):
        self.__treshold = data["treshold"]
        self.__max_distance = data["maxDistance"]
        self.__frame_urls = {}
        self.__frame_boxes = {}

        db_job = Job.objects.select_related('segment__task').get(pk = jid)
        db_segment = db_job.segment
        db_task = db_segment.task

        self.__stop_frame = db_segment.stop_frame

        for root, _, filenames in os.walk(db_task.get_data_dirname()):
            for filename in fnmatch.filter(filenames, '*.jpg'):
                frame = int(os.path.splitext(filename)[0])
                if frame >= db_segment.start_frame and frame <= db_segment.stop_frame:
                    self.__frame_urls[frame] = os.path.join(root, filename)

        for frame in self.__frame_urls:
            self.__frame_boxes[frame] = [box for box in data["boxes"] if box["frame"] == frame]

        IE_PLUGINS_PATH = os.getenv('IE_PLUGINS_PATH', None)
        REID_MODEL_DIR = os.getenv('REID_MODEL_DIR', None)

        if not IE_PLUGINS_PATH:
            raise Exception("Environment variable 'IE_PLUGINS_PATH' isn't defined")
        if not REID_MODEL_DIR:
            raise Exception("Environment variable 'REID_MODEL_DIR' isn't defined")

        REID_XML = os.path.join(REID_MODEL_DIR, "reid.xml")
        REID_BIN = os.path.join(REID_MODEL_DIR, "reid.bin")
        
        self.__plugin = IEPlugin(device="CPU", plugin_dirs=[IE_PLUGINS_PATH])
        network = IENetwork.from_ir(model=REID_XML, weights=REID_BIN)
        self.__input_blob_name = next(iter(network.inputs))
        self.__output_blob_name = next(iter(network.outputs))
        self.__input_height, self.__input_width = network.inputs[self.__input_blob_name].shape[-2:]
        self.__executable_network = self.__plugin.load(network=network)
        del network


    def __del__(self):
        if self.__executable_network:
            del self.__executable_network
            self.__executable_network = None

        if self.__plugin:
            del self.__plugin
            self.__plugin = None


    def __boxes_are_compatible(self, cur_box, next_box):
        cur_c_x = (cur_box["xtl"] + cur_box["xbr"]) / 2
        cur_c_y = (cur_box["ytl"] + cur_box["ybr"]) / 2
        next_c_x = (next_box["xtl"] + next_box["xbr"]) / 2
        next_c_y = (next_box["ytl"] + next_box["ybr"]) / 2
        compatible_distance = euclidean([cur_c_x, cur_c_y], [next_c_x, next_c_y]) <= self.__max_distance
        compatible_label = cur_box["label_id"] == next_box["label_id"]
        return compatible_distance and compatible_label and "path_id" not in next_box


    def __compute_difference(self, image_1, image_2):
        image_1 = cv2.resize(image_1, (self.__input_width, self.__input_height)).transpose((2,0,1))
        image_2 = cv2.resize(image_2, (self.__input_width, self.__input_height)).transpose((2,0,1))

        input_1 = {
            self.__input_blob_name: image_1[numpy.newaxis, ...]
        }

        input_2 = {
            self.__input_blob_name: image_2[numpy.newaxis, ...]
        }

        embedding_1 = self.__executable_network.infer(inputs = input_1)[self.__output_blob_name]
        embedding_2 = self.__executable_network.infer(inputs = input_2)[self.__output_blob_name]

        embedding_1 = embedding_1.reshape(embedding_1.size)
        embedding_2 = embedding_2.reshape(embedding_2.size)

        return cosine(embedding_1, embedding_2)


    def __compute_difference_matrix(self, cur_boxes, next_boxes, cur_image, next_image):
        def _int(number, upper):
            return math.floor(numpy.clip(number, 0, upper - 1))

        default_mat_value = 1000.0

        matrix = numpy.full([len(cur_boxes), len(next_boxes)], default_mat_value, dtype=float)
        for row in range(len(cur_boxes)):
            cur_box = cur_boxes[row]
            cur_width = cur_image.shape[1]
            cur_height = cur_image.shape[0]
            cur_xtl, cur_xbr, cur_ytl, cur_ybr = (
                _int(cur_box["xtl"], cur_width), _int(cur_box["xbr"], cur_width),
                _int(cur_box["ytl"], cur_height), _int(cur_box["ybr"], cur_height)
            )

            for col in range(len(next_boxes)):
                next_box = next_boxes[col]
                next_width = next_image.shape[1]
                next_height = next_image.shape[0]
                next_xtl, next_xbr, next_ytl, next_ybr = (
                    _int(next_box["xtl"], next_width), _int(next_box["xbr"], next_width),
                    _int(next_box["ytl"], next_height), _int(next_box["ybr"], next_height)
                )

                if not self.__boxes_are_compatible(cur_box, next_box):
                    continue

                crop_1 = cur_image[cur_ytl:cur_ybr, cur_xtl:cur_xbr]
                crop_2 = next_image[next_ytl:next_ybr, next_xtl:next_xbr]
                matrix[row][col] = self.__compute_difference(crop_1, crop_2)

        return matrix


    def __apply_matching(self):
        frames = sorted(list(self.__frame_boxes.keys()))
        job = rq.get_current_job()
        box_paths = {}

        for idx, (cur_frame, next_frame) in enumerate(list(zip(frames[:-1], frames[1:]))):
            job.refresh()
            if "cancel" in job.meta:
                return None

            job.meta["progress"] = idx * 100.0 / len(frames)
            job.save_meta()

            cur_boxes = self.__frame_boxes[cur_frame]
            next_boxes = self.__frame_boxes[next_frame]

            for box in cur_boxes:
                if "path_id" not in box:
                    path_id = len(box_paths)
                    box_paths[path_id] = [box]
                    box["path_id"] = path_id

            if not (len(cur_boxes) and len(next_boxes)):
                continue

            cur_image = cv2.imread(self.__frame_urls[cur_frame], cv2.IMREAD_COLOR)
            next_image = cv2.imread(self.__frame_urls[next_frame], cv2.IMREAD_COLOR)
            difference_matrix = self.__compute_difference_matrix(cur_boxes, next_boxes, cur_image, next_image)
            cur_idxs, next_idxs = linear_sum_assignment(difference_matrix)
            for idx in range(len(cur_idxs)):
                if (difference_matrix[cur_idxs[idx]][next_idxs[idx]]) <= self.__treshold:
                    cur_box = cur_boxes[cur_idxs[idx]]
                    next_box = next_boxes[next_idxs[idx]]
                    next_box["path_id"] = cur_box["path_id"]
                    box_paths[cur_box["path_id"]].append(next_box)

        for box in self.__frame_boxes[frames[-1]]:
            if "path_id" not in box:
                path_id = len(box_paths)
                box["path_id"] = path_id
                box_paths[path_id] = [box]

        return box_paths


    def run(self):
        box_paths = self.__apply_matching()
        output = []

        # ReID process has been canceled
        if box_paths is None:
            return

        for path_id in box_paths:
            output.append({
                "label_id": box_paths[path_id][0]["label_id"],
                "group_id": 0,
                "attributes": [],
                "frame": box_paths[path_id][0]["frame"],
                "shapes": box_paths[path_id]
            })

            for box in output[-1]["shapes"]:
                del box["id"]
                del box["path_id"]
                del box["group_id"]
                del box["label_id"]
                box["outside"] = False
                box["attributes"] = []

        for path in output:
            if path["shapes"][-1]["frame"] != self.__stop_frame:
                copy = path["shapes"][-1].copy()
                copy["outside"] = True
                copy["frame"] += 1
                path["shapes"].append(copy)

        return output
