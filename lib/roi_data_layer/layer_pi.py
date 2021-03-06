# --------------------------------------------------------
# Fast R-CNN
# Copyright (c) 2015 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Ross Girshick
# --------------------------------------------------------

"""The data layer used during training to train a Fast R-CNN network.

RoIDataLayerPi implements a Caffe Python layer for reading in multiple images
into the blobs. This is useful for datasets like NYUD2 and UCF 101.
"""

import caffe
from fast_rcnn.config import cfg
from roi_data_layer.minibatch import get_minibatch
import numpy as np
import argparse, os
from multiprocessing import Process, Queue
from IPython.core.debugger import Tracer

class RoIDataLayerPi(caffe.Layer):
    """Fast R-CNN data layer used for training."""

    def _shuffle_roidb_inds(self):
        """Randomly permute the training roidb."""
        valid = []
        for i,r in enumerate(self._roidb):
            ov = r['max_overlaps'][:, np.newaxis]
            has_fg = np.any(np.all(ov > cfg.TRAIN.FG_THRESH, axis = 1), axis = 0)
            has_bg = np.any(np.all(np.hstack((ov > cfg.TRAIN.BG_THRESH_LO, ov < cfg.TRAIN.BG_THRESH_HI)), axis = 1), axis = 0)
            if has_fg and has_bg:
                valid.append(i)
        
        pp = np.random.permutation(np.arange(len(self._roidb)))
        pp = [a for a in pp if a in valid]
        self._perm = pp
        self._cur = 0

    def _get_next_minibatch_inds(self):
        """Return the roidb indices for the next minibatch."""
        if self._cur + cfg.TRAIN.IMS_PER_BATCH >= len(self._perm):
            self._shuffle_roidb_inds()

        db_inds = self._perm[self._cur:self._cur + cfg.TRAIN.IMS_PER_BATCH]
        self._cur += cfg.TRAIN.IMS_PER_BATCH
        return db_inds

    def _get_next_minibatch(self):
        """Return the blobs to be used for the next minibatch.

        If cfg.TRAIN.USE_PREFETCH is True, then blobs will be computed in a
        separate process and made available through self._blob_queue.
        """
        if cfg.TRAIN.USE_PREFETCH:
            return self._blob_queue.get()
        else:
            db_inds = self._get_next_minibatch_inds()
            minibatch_db = [self._roidb[i] for i in db_inds]
            return get_minibatch(minibatch_db, self._num_classes, self._num_data)

    def set_roidb(self, roidb):
        """Set the roidb to be used by this layer during training."""
        self._roidb = roidb
        self._shuffle_roidb_inds()
        if cfg.TRAIN.USE_PREFETCH:
            self._blob_queue = Queue(10)
            self._prefetch_process = BlobFetcher(self._blob_queue,
                                                 self._roidb,
                                                 self._num_classes)
            self._prefetch_process.start()
            # Terminate the child process when the parent exists
            def cleanup():
                print 'Terminating BlobFetcher'
                self._prefetch_process.terminate()
                self._prefetch_process.join()
            import atexit
            atexit.register(cleanup)

    def _parse_args(self, str_arg):
        parser = argparse.ArgumentParser(description='Python Layer Parameters Pi')
        parser.add_argument('--num_classes', default=None, type=int)
        parser.add_argument('--num_data', default=None, type=int)
        args = parser.parse_args(str_arg.split())
        return args

    def setup(self, bottom, top):
        """Setup the RoIDataLayerPi."""

        # parse the layer parameter string, which must be valid YAML
        layer_params = self._parse_args(self.param_str_)

        self._num_classes = layer_params.num_classes
        self._num_data = layer_params.num_data

        self._name_to_top_map = {'data': 0};
        # data blob: holds a batch of N images, each with 3 channels
        # The height and width (100 x 100) are dummy values
        top[0].reshape(1, 7, 100, 100)  # change to 7

        for i in xrange(1, self._num_data):
            self._name_to_top_map['data_{:d}'.format(i)] = i;
            top[i].reshape(1, 3, 100, 100)

        self._name_to_top_map['rois'] = self._num_data;
        # rois blob: holds R regions of interest, each is a 5-tuple
        # (n, x1, y1, x2, y2) specifying an image batch index n and a
        # rectangle (x1, y1, x2, y2)
        top[self._name_to_top_map['rois']].reshape(1, 5)

        self._name_to_top_map['labels'] = self._num_data+1;
        # labels blob: R categorical labels in [0, ..., K] for K foreground
        # classes plus background
        top[self._name_to_top_map['labels']].reshape(1)

        if cfg.TRAIN.BBOX_REG:
            self._name_to_top_map['bbox_targets'] = self._num_data + 2
            # bbox_targets blob: R bounding-box regression targets with 4
            # targets per class
            top[self._name_to_top_map['bbox_targets']].reshape(1, self._num_classes * 4)

            self._name_to_top_map['bbox_loss_weights'] = self._num_data + 3
            # bbox_loss_weights blob: At most 4 targets per roi are active;
            # thisbinary vector sepcifies the subset of active targets
            top[self._name_to_top_map['bbox_loss_weights']].reshape(1, self._num_classes * 4)


    def forward(self, bottom, top):
        """Get blobs and copy them into this layer's top blob vector."""
        blobs = self._get_next_minibatch()

        for blob_name, blob in blobs.iteritems():
            top_ind = self._name_to_top_map[blob_name]
            # Reshape net's input blobs
            top[top_ind].reshape(*(blob.shape))
            # Copy data into net's input blobs
            top[top_ind].data[...] = blob.astype(np.float32, copy=False)

    def backward(self, top, propagate_down, bottom):
        """This layer does not propagate gradients."""
        pass

    def reshape(self, bottom, top):
        """Reshaping happens during the call to forward."""
        pass

class BlobFetcher(Process):
    """Experimental class for prefetching blobs in a separate process."""
    def __init__(self, queue, roidb, num_classes):
        super(BlobFetcher, self).__init__()
        self._queue = queue
        self._roidb = roidb
        self._num_classes = num_classes
        self._perm = None
        self._cur = 0
        self._shuffle_roidb_inds()
        # fix the random seed for reproducibility
        np.random.seed(cfg.RNG_SEED)

    def _shuffle_roidb_inds(self):
        """Randomly permute the training roidb."""
        # TODO(rbg): remove duplicated code
        self._perm = np.random.permutation(np.arange(len(self._roidb)))
        self._cur = 0

    def _get_next_minibatch_inds(self):
        """Return the roidb indices for the next minibatch."""
        # TODO(rbg): remove duplicated code
        if self._cur + cfg.TRAIN.IMS_PER_BATCH >= len(self._roidb):
            self._shuffle_roidb_inds()

        db_inds = self._perm[self._cur:self._cur + cfg.TRAIN.IMS_PER_BATCH]
        self._cur += cfg.TRAIN.IMS_PER_BATCH
        return db_inds

    def run(self):
        print 'BlobFetcher started'
        while True:
            db_inds = self._get_next_minibatch_inds()
            minibatch_db = [self._roidb[i] for i in db_inds]
            blobs = get_minibatch(minibatch_db, self._num_classes)
            self._queue.put(blobs)
