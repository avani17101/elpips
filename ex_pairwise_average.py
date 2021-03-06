# Finds the E-LPIPS average (barycenter) of two images.
#
# Runs the iteration for 100 000 steps. Outputs are generated by default into directory out_bary2,
# but this directory may be changed with --outdir.
#
# The final result will be outdir/100000.png by default.
#
# This code also supports the LPIPS metric to facilitate comparisons.
#
# Usage:
#    python ex_pairwise_average.py image1 image2
#    python ex_pairwise_average.py image1 image2 --metric=[elpips_vgg|lpips_vgg|lpips_squeeze]


import tensorflow as tf
import numpy as np
import pdb
import os
import csv
import itertools 
import time
import sys
import argparse

import elpips

import scipy.misc
import imageio


TOLERANCE = 0.00001 # How far to clip images from 0 and 1.


parser = argparse.ArgumentParser()
parser.add_argument('images', type=str, nargs=2, help='input images to average')
parser.add_argument('--outdir', type=str, default="out_bary2", help='output directory for intermediate files. Default: out_bary2')
parser.add_argument('--steps', type=int, default=100000, help='number of iterations to run')
parser.add_argument('--metric', type=str, default='elpips_vgg', help='(elpips_vgg, lpips_vgg, lpips_squeeze)')
parser.add_argument('--seed', type=int, default=-1, help='random seed (-1 for random)')
parser.add_argument('--learning_rate', type=float, default=0.03, help='step size multiplier for the optimization')


args = parser.parse_args()


if args.metric not in ('elpips_vgg', 'elpips_squeeze_maxpool', 'lpips_vgg', 'lpips_squeeze'):
	raise Exception('Unsupported metric.')


def load_image(path):
	_, ext = os.path.splitext(path)
	if ext.lower() == '.npy':
		image = np.load(path)
	elif ext.lower() in ('.png', '.jpg'):
		image = imageio.imread(path).astype(np.float32) / 255.0
	else:
		raise Exception('Unknown image type.')
		
	return image

	
# Create output directory.
os.makedirs(args.outdir, exist_ok=True)

# Load inputs.
images = []
src_image = load_image(args.images[0])[:,:,0:3]
dest_image = load_image(args.images[1])[:,:,0:3]

images.append(np.expand_dims(src_image, 0))
images.append(np.expand_dims(dest_image, 0))

for i, image in enumerate(images):
	if image.shape != images[0].shape:
		raise Exception("Image '{}' has wrong shape.".format(args.images[i]))

imageio.imwrite(os.path.join(args.outdir, "src_image.png"), (0.5 + 255.0 * src_image).astype(np.uint8))
imageio.imwrite(os.path.join(args.outdir, "dest_image.png"), (0.5 + 255.0 * dest_image).astype(np.uint8))

# Set random seed.
if args.seed >= 0:
	np.random.seed(args.seed)

# Initial image.
init_image = 0.5 + 0.2 * np.random.randn(images[0].shape[0], images[0].shape[1], images[0].shape[2], images[0].shape[3]).astype(np.float32)
init_image = np.clip(init_image, TOLERANCE, 1.0 - TOLERANCE)
imageio.imwrite(os.path.join(args.outdir, "initial_image.png"), (0.5 + 255.0 * init_image[0,:,:,:]).astype(np.uint8))


# Create the graph.
print("Creating graph.")

tf_images = []
for i in range(len(images)):
	tf_images.append(tf.constant(images[i], dtype=tf.float32))

tf_images = tuple(tf_images)


with tf.variable_scope('variables'):
	tf_X = tf.get_variable('tf_X', dtype=tf.float32, initializer=init_image, trainable=True)
	tf_X_uint8 = tf.cast(tf.floor(255.0 * tf.clip_by_value(tf_X, 0.0, 1.0) + 0.5), tf.uint8)[0, :, :, :]


tf_step = tf.get_variable('step', dtype=tf.int32, initializer=0, trainable=False)
tf_increase_step = tf.assign(tf_step, tf_step + 1)

tf_step_f32 = tf.cast(tf_step, tf.float32)
tf_step_f32 = tf.sqrt(100.0 ** 2 + tf_step_f32**2) - 100 # Gradual start.

#Learning rate schedule between 1/t and 1/sqrt(t).
tf_learning_rate = args.learning_rate / (1.0 + 0.02 * tf_step_f32 ** 0.75)


metric_config = elpips.get_config(args.metric)
metric_config.set_scale_levels_by_image_size(image[0].shape[1], image[0].shape[2])
model = elpips.Metric(metric_config)


# Evaluate the distances between the source images and tf_X.
tf_dists = model.forward(tf_images, tf_X) 

# Since tf_image is a tuple, tf_dists is also a tuple.
tf_dist1, tf_dist2 = tf_dists

# Get the distance of the first (and only) elements in the minibatch.
tf_dist1 = tf_dist1[0]
tf_dist2 = tf_dist2[0]

tf_loss = tf.square(tf_dist1) + tf.square(tf_dist2)
	

with tf.control_dependencies([tf_increase_step]):
	tf_optimizer = tf.train.AdamOptimizer(tf_learning_rate)
	tf_minimize = tf_optimizer.minimize(tf_loss)


# Project to a safe distance from invalid colors.
tf_fix_X = tf.assign(tf_X, tf.clip_by_value(tf_X, TOLERANCE, 1.0 - TOLERANCE))


print("Starting session.")

gpu_options = tf.GPUOptions(allow_growth=True)
session_config = tf.ConfigProto(allow_soft_placement=True, log_device_placement=False, gpu_options=gpu_options)
with tf.Session(config=session_config) as sess:
	sess.run([tf.global_variables_initializer(), tf.local_variables_initializer()])

	# No more modifications to the computation graph.
	tf.get_default_graph().finalize()

	# Specify checkpoint for visualizing intermediate results.
	checkpoints = [0, 1, 2, 3, 5, 7, 10, 15, 20, 30, 50, 70, 100, 150, 200, 300, 400, 600, 900, 1200, 1500]
	for i in range(2000, 1 + args.steps, 500):
		checkpoints.append(i)
	checkpoints.append(args.steps)
	checkpoints = set(checkpoints)
	
	# Run the iteration.
	stime = time.time()
	
	for i in range(1 + args.steps):
		sess.run([tf_fix_X])
		
		if i not in checkpoints:
			# Iterate.
			sess.run([tf_minimize])
		else:
			# Also output statistics.
			kernels = []
			ops = []
			results = {}

			kernels.append(tf_loss)
			def op(x):
				results['loss'] = x
			ops.append(op)

			kernels.append(tf_learning_rate)
			def op(x):
				results['learning_rate'] = x
			ops.append(op)
			
			kernels.append(tf_X_uint8)
			def op(x):
				results['X_uint8'] = x
			ops.append(op)
			
			kernels.append(tf_minimize)
			def op(x):
				pass
			ops.append(op)
			
			for x, op in zip(sess.run(kernels), ops):
				op(x)
				
			# Display results.
			loss, X_uint8 = results['loss'], results['X_uint8']
			etime = time.time()
			print("Elapsed: {} s.  Step {}/{}.  Loss: {}.  Learning rate: {}".format(int(etime - stime), i, args.steps, loss, results['learning_rate']))
			
			imageio.imwrite(os.path.join(args.outdir, "{:06d}.png".format(i)), X_uint8)
			
			if i % 10000 == 0:
				X = sess.run([tf_X])
				np.save(os.path.join(args.outdir, "save_{:06d}.npy".format(i)), X)
			