import config
from celery import Celery
import logging
from tensorflow.contrib.slim.python.slim.nets.inception_v3 import inception_v3_base
import random
import time
import json
from flask import Flask, jsonify, request
import numpy as np
from scipy.misc import imread, imresize
import tensorflow as tf
import sys
import os
import copy
import re
from tensorflow.python.platform import gfile
from tensorflow.contrib.layers import *

import align.detect_face
import requests

from distutils.version import LooseVersion

VERSION_GTE_0_12_0 = LooseVersion(tf.__version__) >= LooseVersion('0.12.0')

if VERSION_GTE_0_12_0:
    standardize_image = tf.image.per_image_standardization
else:
    standardize_image = tf.image.per_image_whitening


# This could be added to the Flask configuration
AGE_CHECKPOINT_PATH = 'run-1579/'
GENDER_CHECKPOINT_PATH = '21936/'

RESIZE_AOI = 256
RESIZE_FINAL = 227
MODEL_PATH = 'model'
image_size = 160
margin = 44
gpu_memory_fraction = 1.0
TOWER_NAME = 'tower'

def _activation_summary(x):
    tensor_name = re.sub('%s_[0-9]*/' % TOWER_NAME, '', x.op.name)
    tf.summary.histogram(tensor_name + '/activations', x)
    tf.summary.scalar(tensor_name + '/sparsity', tf.nn.zero_fraction(x))

def inception_v3(nlabels, images, pkeep, is_training):

    batch_norm_params = {
        "is_training": is_training,
        "trainable": True,
        # Decay for the moving averages.
        "decay": 0.9997,
        # Epsilon to prevent 0s in variance.
        "epsilon": 0.001,
        # Collection containing the moving mean and moving variance.
        "variables_collections": {
            "beta": None,
            "gamma": None,
            "moving_mean": ["moving_vars"],
            "moving_variance": ["moving_vars"],
        }
    }
    weight_decay = 0.00004
    stddev=0.1
    weights_regularizer = tf.contrib.layers.l2_regularizer(weight_decay)
    with tf.variable_scope("InceptionV3", "InceptionV3", [images]) as scope:

        with tf.contrib.slim.arg_scope(
                [tf.contrib.slim.conv2d, tf.contrib.slim.fully_connected],
                weights_regularizer=weights_regularizer,
                trainable=True):
            with tf.contrib.slim.arg_scope(
                    [tf.contrib.slim.conv2d],
                    weights_initializer=tf.truncated_normal_initializer(stddev=stddev),
                    activation_fn=tf.nn.relu,
                    normalizer_fn=batch_norm,
                    normalizer_params=batch_norm_params):
                net, end_points = inception_v3_base(images, scope=scope)
                with tf.variable_scope("logits"):
                    shape = net.get_shape()
                    net = avg_pool2d(net, shape[1:3], padding="VALID", scope="pool")
                    net = tf.nn.dropout(net, pkeep, name='droplast')
                    net = flatten(net, scope="flatten")

    with tf.variable_scope("output") as scope:
        weights = tf.Variable(tf.truncated_normal([2048, nlabels], mean=0.0, stddev=0.01), name='weights')
        biases = tf.Variable(tf.constant(0.0, shape=[nlabels], dtype=tf.float32), name='biases')
        output = tf.add(tf.matmul(net, weights), biases, name=scope.name)
        _activation_summary(output)
    return output

def _is_png(filename):
    """Determine if a file contains a PNG format image.
    Args:
    filename: string, path of the image file.
    Returns:
    boolean indicating if the image is a PNG.
    """
    return '.png' in filename

def make_celery(app):
    celery = Celery(app.import_name, broker=config.CELERY_BROKER)
    celery.conf.update(app.config)
    TaskBase = celery.Task

    class ContextTask(TaskBase):
        abstract = True

        def __call__(self, *args, **kwargs):
            with app.app_context():
                return TaskBase.__call__(self, *args, **kwargs)
    celery.Task = ContextTask

    return celery

face = Flask(__name__)

#face.register_blueprint(api)

face.config.from_object('config')
celery = make_celery(face)


def prewhiten(x):
    mean = np.mean(x)
    std = np.std(x)
    std_adj = np.maximum(std, 1.0/np.sqrt(x.size))
    y = np.multiply(np.subtract(x, mean), 1/std_adj)
    return y


def load_and_align_data(image_paths):

    minsize = 20 # minimum size of face
    threshold = [ 0.6, 0.7, 0.7 ]  # three steps's threshold
    factor = 0.709 # scale factor

    print('Creating networks and loading parameters')
    with tf.Graph().as_default():
        gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=gpu_memory_fraction)
        sess = tf.Session(config=tf.ConfigProto(gpu_options=gpu_options, log_device_placement=False))
        with sess.as_default():
            pnet, rnet, onet = align.detect_face.create_mtcnn(sess, None)

    tmp_image_paths=copy.copy(image_paths)
    img_list = []
    for image in tmp_image_paths:
        #image_path = os.path.join(image, image)
        #print (image_path)
        img = imread(os.path.expanduser(image), mode='RGB')
        img_size = np.asarray(img.shape)[0:2]
        bounding_boxes, _ = align.detect_face.detect_face(img, minsize, pnet, rnet, onet, threshold, factor)
        if len(bounding_boxes) < 1:
          print("can't detect face, remove ", image)
          continue
        det = np.squeeze(bounding_boxes[0,0:4])
        bb = np.zeros(4, dtype=np.int32)
        bb[0] = np.maximum(det[0]-margin/2, 0)
        bb[1] = np.maximum(det[1]-margin/2, 0)
        bb[2] = np.minimum(det[2]+margin/2, img_size[1])
        bb[3] = np.minimum(det[3]+margin/2, img_size[0])
        cropped = img[bb[1]:bb[3],bb[0]:bb[2],:]
        aligned = imresize(cropped, (image_size, image_size), interp='bilinear')
        prewhitened = prewhiten(aligned)
        img_list.append(prewhitened)
    images = []
    if img_list:
    	images = np.stack(img_list)
    return images


def make_multi_crop_batch(filename, coder):
    """Process a single image file.

 Args:
    filename: string, path to an image file e.g., '/path/to/example.JPG'.
    coder: instance of ImageCoder to provide TensorFlow image coding utils.
    Returns:
    image_buffer: string, JPEG encoding of RGB image.
    """
    # Read the image file.
    with tf.gfile.FastGFile(filename, 'rb') as f:
        image_data = f.read()

    # Convert any PNG to JPEG's for consistency.
    if _is_png(filename):
        print('Converting PNG to JPEG for %s' % filename)
        image_data = coder.png_to_jpeg(image_data)

    image = coder.decode_jpeg(image_data)

    crops = []
    print('Running multi-cropped image')
    h = image.shape[0]
    w = image.shape[1]
    hl = h - RESIZE_FINAL
    wl = w - RESIZE_FINAL

    crop = tf.image.resize_images(image, (RESIZE_FINAL, RESIZE_FINAL))
    crops.append(standardize_image(crop))
    crops.append(tf.image.flip_left_right(crop))

    corners = [ (0, 0), (0, wl), (hl, 0), (hl, wl), (int(hl/2), int(wl/2))]
    for corner in corners:
        ch, cw = corner
        cropped = tf.image.crop_to_bounding_box(image, ch, cw, RESIZE_FINAL, RESIZE_FINAL)
        crops.append(standardize_image(cropped))
        flipped = tf.image.flip_left_right(cropped)
        crops.append(standardize_image(flipped))

    image_batch = tf.stack(crops)
    return image_batch


class ImageCoder(object):

    def __init__(self):
        # Create a single Session to run all image coding calls.
        config = tf.ConfigProto(allow_soft_placement=True)
        self._sess = tf.Session(config=config)

        # Initializes function that converts PNG to JPEG data.
        self._png_data = tf.placeholder(dtype=tf.string)
        image = tf.image.decode_png(self._png_data, channels=3)
        self._png_to_jpeg = tf.image.encode_jpeg(image, format='rgb', quality=100)

        # Initializes function that decodes RGB JPEG data.
        self._decode_jpeg_data = tf.placeholder(dtype=tf.string)
        self._decode_jpeg = tf.image.decode_jpeg(self._decode_jpeg_data, channels=3)
        self.crop = tf.image.resize_images(self._decode_jpeg, (RESIZE_AOI, RESIZE_AOI))

    def png_to_jpeg(self, image_data):
        return self._sess.run(self._png_to_jpeg,
                              feed_dict={self._png_data: image_data})

    def decode_jpeg(self, image_data):
        image = self._sess.run(self.crop, #self._decode_jpeg,
                               feed_dict={self._decode_jpeg_data: image_data})

        assert len(image.shape) == 3
        assert image.shape[2] == 3
        return image

def levi_hassner(nlabels, images, pkeep, is_training):
    weight_decay = 0.0005
    weights_regularizer = tf.contrib.layers.l2_regularizer(weight_decay)
    with tf.variable_scope("LeviHassner", "LeviHassner", [images]) as scope:

        with tf.contrib.slim.arg_scope(
                [convolution2d, fully_connected],
                weights_regularizer=weights_regularizer,
                biases_initializer=tf.constant_initializer(1.),
                weights_initializer=tf.random_normal_initializer(stddev=0.005),
                trainable=True):
            with tf.contrib.slim.arg_scope(
                    [convolution2d],
                    weights_initializer=tf.random_normal_initializer(stddev=0.01)):

                conv1 = convolution2d(images, 96, [7,7], [4, 4], padding='VALID', biases_initializer=tf.constant_initializer(0.), scope='conv1')
                pool1 = max_pool2d(conv1, 3, 2, padding='VALID', scope='pool1')
                norm1 = tf.nn.local_response_normalization(pool1, 5, alpha=0.0001, beta=0.75, name='norm1')
                conv2 = convolution2d(norm1, 256, [5, 5], [1, 1], padding='SAME', scope='conv2')
                pool2 = max_pool2d(conv2, 3, 2, padding='VALID', scope='pool2')
                norm2 = tf.nn.local_response_normalization(pool2, 5, alpha=0.0001, beta=0.75, name='norm2')
                conv3 = convolution2d(norm2, 384, [3, 3], [1, 1], biases_initializer=tf.constant_initializer(0.), padding='SAME', scope='conv3')
                pool3 = max_pool2d(conv3, 3, 2, padding='VALID', scope='pool3')
                flat = tf.reshape(pool3, [-1, 384*6*6], name='reshape')
                full1 = fully_connected(flat, 512, scope='full1')
                drop1 = tf.nn.dropout(full1, pkeep, name='drop1')
                full2 = fully_connected(drop1, 512, scope='full2')
                drop2 = tf.nn.dropout(full2, pkeep, name='drop2')

    with tf.variable_scope("output") as scope:

        weights = tf.Variable(tf.random_normal([512, nlabels], mean=0.0, stddev=0.01), name='weights')
        biases = tf.Variable(tf.constant(0.0, shape=[nlabels], dtype=tf.float32), name='biases')
        output = tf.add(tf.matmul(drop2, weights), biases, name=scope.name)
    return output


def get_model_filenames(model_dir):
    files = os.listdir(model_dir)
    meta_files = [s for s in files if s.endswith('.meta')]
    if len(meta_files)==0:
        raise ValueError('No meta file found in the model directory (%s)' % model_dir)
    elif len(meta_files)>1:
        raise ValueError('There should not be more than one meta file in the model directory (%s)' % model_dir)
    meta_file = meta_files[0]
    ckpt = tf.train.get_checkpoint_state(model_dir)
    if ckpt and ckpt.model_checkpoint_path:
        ckpt_file = os.path.basename(ckpt.model_checkpoint_path)
        return meta_file, ckpt_file

    meta_files = [s for s in files if '.ckpt' in s]
    max_step = -1
    for f in files:
        step_str = re.match(r'(^model-[\w\- ]+.ckpt-(\d+))', f)
        if step_str is not None and len(step_str.groups())>=2:
            step = int(step_str.groups()[1])
            if step > max_step:
                max_step = step
                ckpt_file = step_str.groups()[0]
    return meta_file, ckpt_file



def load_model(model, input_map=None):
    # Check if the model is a model directory (containing a metagraph and a checkpoint file)
    #  or if it is a protobuf file with a frozen graph
    model_exp = os.path.expanduser(model)
    if (os.path.isfile(model_exp)):
        print('Model filename: %s' % model_exp)
        with gfile.FastGFile(model_exp,'rb') as f:
            graph_def = tf.GraphDef()
            graph_def.ParseFromString(f.read())
            tf.import_graph_def(graph_def, input_map=input_map, name='')
    else:
        print('Model directory: %s' % model_exp)
        meta_file, ckpt_file = get_model_filenames(model_exp)

        print('Metagraph file: %s' % meta_file)
        print('Checkpoint file: %s' % ckpt_file)

        saver = tf.train.import_meta_graph(os.path.join(model_exp, meta_file), input_map=input_map)
        saver.restore(tf.get_default_session(), os.path.join(model_exp, ckpt_file))


@face.route('/face_align', methods=['post'])
def route_face_align():
    data = request.json
    image_path = data['image_path']
    image_id = data['image_id']
    callback = data['callback_url']
    face.logger.info("Classifying image %s" % (image_path),)
    process_face_align.apply_async((image_id, image_path,callback,))
    return jsonify({'message': 'Image was register succesfully, Result will be return soon.'})


@face.route('/face_age', methods=['post'])
def route_face_age():
    data = request.json
    image_path = data['image_path']
    image_id = data['image_id']
    callback = data['callback_url']
    face.logger.info("Classifying image %s" % (image_path),)
    process_face_age.apply_async((image_id, image_path,callback,))
    return jsonify({'message': 'Image was register succesfully, Result will be return soon.'})

@face.route('/face_gender', methods=['post'])
def route_face_gender():
    data = request.json
    image_path = data['image_path']
    image_id = data['image_id']
    callback = data['callback_url']
    face.logger.info("Classifying image %s" % (image_path),)
    process_face_gender.apply_async((image_id, image_path,callback,))
    return jsonify({'message': 'Image was register succesfully, Result will be return soon.'})

@celery.task(name='process_face_gender')
def process_face_gender(image_id, image_file, callback_url):
    #config = tf.ConfigProto(allow_soft_placement=True)
    tf.reset_default_graph()
    with tf.Session() as sess:
        label_list = ['M','F']
        nlabels = len(label_list)
        with tf.device('/cpu:0'):
            images = tf.placeholder(tf.float32, [None, RESIZE_FINAL, RESIZE_FINAL, 3])
            logits = inception_v3(nlabels, images, 1, False)
            checkpoint_path = GENDER_CHECKPOINT_PATH


            init = tf.global_variables_initializer()


            ckpt = tf.train.get_checkpoint_state(checkpoint_path)
            if not (ckpt and ckpt.model_checkpoint_path):
                print('No checkpoint file found at [%s]' % checkpoint_path)
                exit(-1)
            # Restore checkpoint as described in top of this program
            print(ckpt.model_checkpoint_path)
            global_step = ckpt.model_checkpoint_path.split('/')[-1].split('-')[-1]
            saver = tf.train.Saver()
            saver.restore(sess, ckpt.model_checkpoint_path)
            softmax_output = tf.nn.softmax(logits)
            coder = ImageCoder()
            image_batch = make_multi_crop_batch(image_file, coder)
            batch_results = sess.run(softmax_output, feed_dict={images:image_batch.eval()})
            output = batch_results[0]
            batch_sz = batch_results.shape[0]

            for i in range(1, batch_sz):
                output = output + batch_results[i]

            output /= batch_sz
            best = np.argmax(output)
            best_choice = (label_list[best], output[best])
            print('Guess @ 1 %s, prob = %.2f' % best_choice)

            data = {
               'image_id': image_id,
               'image_path': image_file,
               'result_gender': [(label_list[best], str(output[best]))]
            }
            requests.post(callback_url, json=data)


@celery.task(name='process_face_age')
def process_face_age(image_id, image_file, callback_url):
    #config = tf.ConfigProto(allow_soft_placement=True)
    tf.reset_default_graph()
    with tf.Session() as sess:
        label_list = ['(0, 2)','(4, 6)','(8, 12)','(15, 20)','(25, 32)','(38, 43)','(48, 53)','(60, 100)']
        nlabels = len(label_list)
        with tf.device('/cpu:0'):
            images = tf.placeholder(tf.float32, [None, RESIZE_FINAL, RESIZE_FINAL, 3])
            logits = levi_hassner(nlabels, images, 1, False)
            checkpoint_path = AGE_CHECKPOINT_PATH
          

            init = tf.global_variables_initializer()


            ckpt = tf.train.get_checkpoint_state(checkpoint_path)
            if not (ckpt and ckpt.model_checkpoint_path):
                print('No checkpoint file found at [%s]' % checkpoint_path)
                exit(-1)
            # Restore checkpoint as described in top of this program
            print(ckpt.model_checkpoint_path)
            global_step = ckpt.model_checkpoint_path.split('/')[-1].split('-')[-1]
            saver = tf.train.Saver()
            saver.restore(sess, ckpt.model_checkpoint_path)
            softmax_output = tf.nn.softmax(logits)
            coder = ImageCoder()
            image_batch = make_multi_crop_batch(image_file, coder)

            batch_results = sess.run(softmax_output, feed_dict={images:image_batch.eval()})
            output = batch_results[0]
            batch_sz = batch_results.shape[0]

            for i in range(1, batch_sz):
                output = output + batch_results[i]

            output /= batch_sz
            best = np.argmax(output)
            best_choice = (label_list[best], output[best])
            print('Guess @ 1 %s, prob = %.2f' % best_choice)
            
            second_best = False
            if nlabels > 2:
                output[best] = 0
                second_best = np.argmax(output)
                print('Guess @ 2 %s, prob = %.2f' % (label_list[second_best], output[second_best]))

            data = {
               'image_id': image_id,
               'image_path': image_file,
               'result_age': [(label_list[best], str(output[best])), (label_list[second_best], str(output[second_best]))]
            }
            requests.post(callback_url, json=data)


@celery.task(name='process_face_align')
def process_face_align(image_id, image_path, callback_url):
    images = load_and_align_data([image_path])
    if not len(images):
        requests.post(callback_url, json={'is_not_face': True, 'error': 'image is not face', 'image_id': image_id, 'image_path': image_path})
        return
    # Get the predictions (output of the softmax) for this image
    t = time.time()
    with tf.Graph().as_default():
        with tf.Session() as sess:
            load_model(MODEL_PATH)

            images_placeholder = tf.get_default_graph().get_tensor_by_name("input:0")
            embeddings = tf.get_default_graph().get_tensor_by_name("embeddings:0")
            phase_train_placeholder = tf.get_default_graph().get_tensor_by_name("phase_train:0")

            feed_dict = {images_placeholder: images, phase_train_placeholder: False}
            emb = sess.run(embeddings, feed_dict=feed_dict)
            dt = time.time() - t
            face.logger.info("Execution time: %0.2f" % (dt * 1000.))
    
            # Single image in this batch
            predictions = emb[0]
    
            print (predictions)
            data = {
               'image_id': image_id,
               'image_path': image_path,
               'result_align': predictions.tolist()
            }
            requests.post(callback_url, json=data)


if __name__ == '__main__':
    print ("I am Started...")
    face.run(debug=True, port=8009)
