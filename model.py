from __future__ import print_function

import cv2
import tensorflow as tf
import numpy as np
import os
import DeSubpixelConv2d

class Edsr:

    def __init__(self, B, F, scale):
        self.B = B
        self.F = F
        self.scale = scale
        self.global_step = tf.placeholder(tf.int32, shape=[], name="global_step")
        self.scaling_factor = 0.1
        self.bias_initializer = tf.constant_initializer(value=0.0)
        self.PS = 3 * (scale*scale) #channels x scale^2
        self.xavier = tf.contrib.layers.xavier_initializer()

        # -- Filters & Biases --
        self.resFilters = list()
        self.resBiases = list()

        for i in range(0, B*2):
            self.resFilters.append( tf.get_variable("resFilter%d" % (i), shape=[3,3,F,F], initializer=self.xavier))
            self.resBiases.append(tf.get_variable(name="resBias%d" % (i), shape=[F], initializer=self.bias_initializer))

        self.filter_one = tf.get_variable("resFilter_one", shape=[3,3,3,F], initializer=self.xavier)
        self.filter_two = tf.get_variable("resFilter_two", shape=[3,3,F,F], initializer=self.xavier)
        self.filter_three = tf.get_variable("resFilter_three", shape=[3,3,F,self.PS], initializer=self.xavier)

        self.bias_one = tf.get_variable(shape=[F], initializer=self.bias_initializer, name="BiasOne")
        self.bias_two = tf.get_variable(shape=[F], initializer=self.bias_initializer, name="BiasTwo")
        self.bias_three = tf.get_variable(shape=[self.PS], initializer=self.bias_initializer, name="BiasThree")


    def model(self, x, y, lr):


        # -- Model architecture --



        out1 = tf.identity(x)
        #downsample
        x = downsample(x, n_feats, scale, conv_type, downsample_type)

        # all residual blocks
        for i in range(self.B):
            x = self.resBlock(x, (i*2))

        
        x = tf.nn.conv2d(x, filter=self.filter_two, strides=[1, 1, 1, 1], padding='SAME')
        x = x + self.bias_two

        # upsample via sub-pixel, equivalent to depth to space
        x = tf.nn.conv2d(x, filter=self.filter_three, strides=[1, 1, 1, 1], padding='SAME')
        x = x + self.bias_three
        out = tf.depth_to_space(x, self.scale, data_format='NHWC', name="NHWC_output")
        x = out + out1

        # -- --

        # some outputs
        out_nchw = tf.transpose(out, [0, 3, 1, 2], name="NCHW_output")
        psnr = tf.image.psnr(out, y, max_val=255.0)
        loss = tf.losses.absolute_difference(out, y) #L1
        ssim = tf.image.ssim(out, y, max_val=255.0)
        
        # (decaying) learning rate
        lr = tf.train.exponential_decay(lr,
                                        self.global_step,
                                        decay_steps=15000,
                                        decay_rate=0.95,
                                        staircase=True)
        # gradient clipping
        optimizer = tf.train.AdamOptimizer(lr)
        gradients, variables = zip(*optimizer.compute_gradients(loss))
        gradients, _ = tf.clip_by_global_norm(gradients, 5.0)
        train_op = optimizer.apply_gradients(zip(gradients, variables))

        return out, loss, train_op, psnr, ssim, lr

    def conv(x, in_feats, out_feats, kernel_sizes=(3, 3), strides=(1, 1), norm=None, act=None, conv_type='default',
             name='conv'):
        with tf.variable_scope(name):
            if conv_type == 'default':
                x = Conv2d(x, out_feats, kernel_sizes, strides, act=act,
                           W_init=init(in_feats, kernel_sizes[0]),
                           b_init=init(in_feats, kernel_sizes[0]))

            elif conv_type == 'depth_wise':
                x = DepthwiseConv2d(x, kernel_sizes, strides, act=tf.nn.relu, W_init=init(in_feats),
                                    b_init=init(in_feats), name='depthwise')
                x = Conv2d(x, out_feats, (1, 1), (1, 1), act=act,
                           W_init=init(in_feats, kernel_sizes[0]),
                           b_init=init(in_feats, kernel_sizes[0]), name='conv')

            else:
                raise Exception('Unknown conv type', conv_type)
        return x

    def downsample(x, n_feats, scale=2, conv_type='default', sample_type='subpixel', name='downsample'):
        with tf.variable_scope(name):
            if sample_type == 'desubpixel':
                assert scale == 2 or scale == 4
                if scale == 2:
                    x = conv(x, 3, n_feats // 4, (1, 1), act=None, conv_type=conv_type, name='conv')
                    x = DeSubpixelConv2d(x, 2, name='pixel_deshuffle')
                else:
                    x = DeSubpixelConv2d(x, 2, name='pixel_deshuffle1')
                    x = conv(x, 12, n_feats // 4, (1, 1), act=None, conv_type=conv_type, name='conv2')
                    x = DeSubpixelConv2d(x, 2, name='pixel_deshuffle2')
        return x


    def resBlock(self, inpt, f_nr):
        x = tf.nn.conv2d(inpt, filter=self.resFilters[f_nr], strides=[1, 1, 1, 1], padding='SAME')
        x = x + self.resBiases[f_nr]
        x=tfa.layers.InstanceNormalization(x)
        x = tf.nn.relu(x)

        x = tf.nn.conv2d(x, filter=self.resFilters[f_nr+1], strides=[1, 1, 1, 1], padding='SAME')
        x = x + self.resBiases[f_nr+1]
        x=tfa.layers.InstanceNormalization(x)
        x = tf.nn.relu(x)
        x = x * self.scaling_factor

        return inpt + x