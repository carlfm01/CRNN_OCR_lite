import os
import re
import pickle
import operator 
from collections import OrderedDict

import cv2
import numpy as np
from numpy.random import RandomState
from PIL import Image, ImageDraw
from tqdm import tqdm

from keras.layers import Conv2D, MaxPooling2D, Activation, Dropout, add, \
                         Dense, Input, Lambda, Bidirectional, ZeroPadding2D, concatenate, \
                         concatenate, multiply, ReLU, DepthwiseConv2D, TimeDistributed
#from keras.layers import LSTM, GRU
from keras.layers import CuDNNLSTM as LSTM
from keras.layers import CuDNNGRU as GRU
from keras.layers.core import *
from keras.layers.normalization import BatchNormalization
from keras.callbacks import Callback
from keras.models import Model, load_model, model_from_json
from keras import optimizers
from keras import backend as K

###############################################################################################
#                                       REFERENCES                                            #
###############################################################################################

# https://github.com/meijieru/crnn.pytorch
# https://github.com/sbillburg/CRNN-with-STN/blob/master/CRNN_with_STN.py
# https://github.com/keras-team/keras/blob/master/examples/image_ocr.py
# https://github.com/keras-team/keras-applications/blob/master/keras_applications/mobilenet.py
#
# Slices RNN:
# https://github.com/zepingyu0512/srnn/blob/master/SRNN.py
#
# Attention block:
# https://github.com/philipperemy/keras-attention-mechanism.git

###############################################################################################
# LOGS:
###############################################################################################

# - casual convs: 8857k params; 16 epochs, 500k training examples: ctc_loss - ~0.91
# - depthwise separable convs and dropouts: 2785k params; 20 epochs, 500k training 
#   examples; 3181s; 429ms/step; 63592 s.; ctc_loss: ~0.85
# 

###############################################################################################

###############################################################################################
# TO DO:
###############################################################################################

# - add bidirectional sliced lstm to make net much more faster;
# - problem with N batches in inference mode - why it's need to multiply by 2 the 
# number of steps?;
# - get distribution on word length on test set;
# - create custom metric for levenstein distances <= 1;

###############################################################################################

class CRNN:

    def __init__(self, num_classes=97, max_string_len=23, shape=(40,40,1), attention=False, time_dense_size=128,
                       GRU=False, n_units=256, single_attention_vector=True):

        self.num_classes = num_classes
        self.shape = shape
        self.attention = attention
        self.max_string_len = max_string_len
        self.n_units = n_units
        self.GRU = GRU
        self.time_dense_size = time_dense_size
        self.single_attention_vector = single_attention_vector

    def depthwise_conv_block(self, inputs, pointwise_conv_filters, conv_size=(3, 3), pooling=None):
        x = DepthwiseConv2D((3, 3), padding='same', strides=(1, 1), depth_multiplier=1, use_bias=False)(inputs)
        x = BatchNormalization(axis=-1)(x)
        x = ReLU(6.)(x)
        x = Conv2D(pointwise_conv_filters, (1, 1), strides=(1, 1), padding='same', use_bias=False)(x)
        x = BatchNormalization(axis=-1)(x)
        x = ReLU(6.)(x)
        if pooling is not None:
            x = MaxPooling2D(pooling)(x)
            if pooling[0] == 2:
                self.pooling_counter_h += 1
            if pooling[1] == 2:
                self.pooling_counter_w += 1
        return Dropout(0.1)(x)

    def SGRU(self, go_backwards):
        input1 = Input(shape=(length, self.time_dense_size), dtype='int32')
        gru1 = GRU(self.n_units, return_sequences=False, go_backwards=go_backwards)(input1)
        Encoder1 = Model(input1, gru1)

        input2 = Input(shape=(2, length, self.time_dense_size), dtype='int32')
        embed2 = TimeDistributed(Encoder1)(input2)
        gru2 = GRU(self.n_units, return_sequences=False, go_backwards=go_backwards)(embed2)
        Encoder2 = Model(input2,gru2)

        input3 = Input(shape=(2, 2, length, self.time_dense_size), dtype='int32')
        embed3 = TimeDistributed(Encoder2)(input3)
        gru3 = GRU(self.n_units, return_sequences=True, go_backwards=go_backwards)(embed3)
        return Model(input3, gru3)

    def SLSTM(self, go_backwards):
        input1 = Input(shape=(2, self.time_dense_size))
        gru1 = LSTM(self.n_units, return_sequences=False, go_backwards=go_backwards)(input1)
        Encoder1 = Model(input1, gru1)

        input2 = Input(shape=(2, 2, self.time_dense_size))
        embed2 = TimeDistributed(Encoder1)(input2)
        gru2 = LSTM(self.n_units, return_sequences=False, go_backwards=go_backwards)(embed2)
        Encoder2 = Model(input2,gru2)

        input3 = Input(shape=(2, 2, 13, self.time_dense_size))
        embed3 = TimeDistributed(Encoder2)(input3)
        gru3 = LSTM(self.n_units, return_sequences=True, go_backwards=go_backwards)(embed3)
        return Model(input3, gru3)

    def get_model(self):
        self.pooling_counter_h, self.pooling_counter_w = 0, 0
        inputs = Input(name='the_input', shape=self.shape, dtype='float32') #100x32x1
        x = ZeroPadding2D(padding=(2, 2))(inputs) #104x36x1
        x = self.depthwise_conv_block(x, 64, conv_size=(3, 3), pooling=None)
        x = self.depthwise_conv_block(x, 128, conv_size=(3, 3), pooling=None)
        x = self.depthwise_conv_block(x, 256, conv_size=(3, 3), pooling=(2, 2))  #52x18x256
        x = self.depthwise_conv_block(x, 256, conv_size=(3, 3), pooling=None)
        x = self.depthwise_conv_block(x, 512, conv_size=(3, 3), pooling=(1, 2))  #52x9x512
        x = self.depthwise_conv_block(x, 512, conv_size=(3, 3), pooling=None)
        x = self.depthwise_conv_block(x, 512, conv_size=(3, 3), pooling=None)

        conv_to_rnn_dims = ((self.shape[0]+4) // (2 ** self.pooling_counter_h), ((self.shape[1]+4) // (2 ** self.pooling_counter_w)) * 512)
        x = Reshape(target_shape=conv_to_rnn_dims, name='reshape')(x) #52x4608
        x = Dense(self.time_dense_size, activation='relu', name='dense1')(x) #52x128 (time_dense_size)
        x = Dropout(0.4)(x)

        # #bidirectional SLICED_LSTM
        ####################################################################################################################

        # x = Reshape(target_shape=(13,2,2,self.time_dense_size), name='reshape')(x) #2x2x13x128 (time_dense_size)

        # if self.GRU:
        #     rnn_1 = self.SRNN(go_backwards=False)(x)
        #     rnn_1b = self.SRNN(go_backwards=True)(x)
        #     rnns_1_merged = add([rnn_1, rnn_1b])
        #     rnn_2 = self.SRNN(go_backwards=False)(rnns_1_merged)
        #     rnn_2b = self.SRNN(go_backwards=True)(rnns_1_merged)
        # else:
        #     rnn_1 = self.SLSTM(go_backwards=False)(x)
        #     rnn_1b = self.SLSTM(go_backwards=True)(x)
        #     rnns_1_merged = add([rnn_1, rnn_1b])

        #     print(rnns_1_merged.shape)

        #     rnn_2 = self.SLSTM(go_backwards=False)(rnns_1_merged)
        #     rnn_2b = self.SLSTM(go_backwards=True)(rnns_1_merged)

        # rnns_2_merged = concatenate([rnn_2, rnn_2b], axis=1)
        # x = Dropout(0.2)(rnns_2_merged)

        ####################################################################################################################

        if not self.GRU:    
            x = Bidirectional(LSTM(self.n_units, return_sequences=True, kernel_initializer='he_normal'), merge_mode='sum', weights=None)(x)
            x = Bidirectional(LSTM(self.n_units, return_sequences=True, kernel_initializer='he_normal'), merge_mode='concat', weights=None)(x)
        else:
            x = Bidirectional(GRU(self.n_units, return_sequences=True, kernel_initializer='he_normal'), merge_mode='sum', weights=None)(x)
            x = Bidirectional(GRU(self.n_units, return_sequences=True, kernel_initializer='he_normal'), merge_mode='concat', weights=None)(x)
        x = Dropout(0.2)(x)

        if self.attention:
            x = attention_3d_block(x, time_steps=conv_to_rnn_dims[0], single_attention_vector=self.single_attention_vector)

        x_ctc = Dense(self.num_classes, kernel_initializer='he_normal', name='dense2')(x)
        y_pred = Activation('softmax', name='softmax')(x_ctc)

        labels = Input(name='the_labels', shape=[self.max_string_len], dtype='float32')
        input_length = Input(name='input_length', shape=[1], dtype='int64')
        label_length = Input(name='label_length', shape=[1], dtype='int64')

        loss_out = Lambda(ctc_lambda_func, output_shape=(1,), name='ctc')([y_pred, labels, input_length, label_length])
        outputs = [loss_out]

        model = Model(inputs=[inputs, labels, input_length, label_length], outputs=outputs)
        return model

def ctc_lambda_func(args):
    y_pred, labels, input_length, label_length = args
    # the 2 is critical here since the first couple outputs of the RNN tend to be garbage:

    y_pred = y_pred[:, 2:, :]
    return K.ctc_batch_cost(labels, y_pred, input_length, label_length)

def levenshtein(seq1, seq2):  
    size_x = len(seq1) + 1
    size_y = len(seq2) + 1

    matrix = np.zeros((size_x, size_y))
    for x in range(size_x):
        matrix [x, 0] = x
    for y in range(size_y):
        matrix [0, y] = y

    for x in range(1, size_x):
        for y in range(1, size_y):
            if seq1[x-1] == seq2[y-1]:
                matrix [x,y] = min(
                    matrix[x-1, y] + 1,
                    matrix[x-1, y-1],
                    matrix[x, y-1] + 1
                )
            else:
                matrix [x,y] = min(
                    matrix[x-1,y] + 1,
                    matrix[x-1,y-1] + 1,
                    matrix[x,y-1] + 1
                )
    return (matrix[size_x - 1, size_y - 1])

def edit_distance(y_pred, y_true):
    c = {}
    for y0, y in zip(y_pred, y_true):
        distance = levenshtein(y0, y)
        if distance not in c.keys():
            c[distance] = 1
        c[distance] += 1
    c = {k:v/len(y_true) for k, v in c.items()}
    return c

def load_model_custom(path, weights="model"):
    json_file = open(path+'/model.json', 'r')
    loaded_model_json = json_file.read()    
    json_file.close()
    loaded_model = model_from_json(loaded_model_json)
    loaded_model.load_weights(path+"/%s.h5" % weights)
    return loaded_model

def init_predictor(model):
    return Model(inputs=model.input, outputs=model.get_layer('softmax').output)

def labels_to_text(labels, inverse_classes=None):
    ret = []
    for c in labels:
        if c == len(inverse_classes) or c == -1:
            ret.append("")
        else:
            ret.append(str(inverse_classes[c]))
    return "".join(ret)

def decode_predict_ctc(out=None, a=None, predictor=None, top_paths=1, beam_width=5, inverse_classes=None):
    if out is None:
        c = np.expand_dims(a.T, axis=0)
        out = predictor.predict(c)
    results = []
    if beam_width < top_paths:
        beam_width = top_paths
    for i in tqdm(range(out.shape[0])):
        text = ""
        for j in range(top_paths):
            inp = np.expand_dims(out[i], axis=0)
            labels = K.get_value(K.ctc_decode(inp, input_length=np.ones(inp.shape[0])*inp.shape[1],
                               greedy=False, beam_width=beam_width, top_paths=top_paths)[0][j])[0]
            text += labels_to_text(labels, inverse_classes)
        results.append(text)
    return results

class Readf:

    def __init__(self, path, img_size=(40,40), trsh=100, normed=False, fill=255, offset=5, mjsynth=False,
                    random_state=None, length_sort_mode='target', batch_size=32, classes=None):
        self.trsh = trsh
        self.mjsynth = mjsynth
        self.path = path
        self.batch_size = batch_size
        self.img_size = img_size
        self.flag = False
        self.offset = offset
        self.fill = fill
        self.normed = normed
        if self.mjsynth:
            self.names = open(path+"/imlist.txt", "r").readlines()[:1000000] #<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<<
        else:
            self.names = [os.path.join(dp, f) for dp, dn, filenames in os.walk(path) 
                for f in filenames if re.search('png|jpeg|jpg', f)]
        self.length = len(self.names)
        self.prng = RandomState(random_state)
        self.prng.shuffle(self.names)
        self.classes = classes

        lengths = get_lengths(self.names)
        if not self.mjsynth:
            self.targets = pickle.load(open(self.path+'/target.pickle.dat', 'rb'))
            self.max_len = max([i.shape[0] for i in self.targets.values()])
        else:
            self.mean, self.std = pickle.load(open(self.path+'/mean_std.pickle.dat', 'rb'))
            self.max_len = max(lengths.values())
        self.blank = len(self.classes)

        #reorder pictures by ascending of seq./pic length
        if not self.mjsynth:
            if length_sort_mode == 'target':
                self.names = OrderedDict(sorted([(k,len(self.targets[self.parse_name(k)])) for k in self.names], key=operator.itemgetter(1), reverse=False))
            elif length_sort_mode == 'shape':
                self.names = OrderedDict(sorted([(k,lengths[self.parse_name(k)]) for k in self.names], key=operator.itemgetter(1), reverse=False))
        else:
            self.names = OrderedDict(sorted([(k,lengths[k]) for k in self.names], key=operator.itemgetter(1), reverse=False))

        print(' [INFO] Reader initialized with params: %s' % ('n_images: %i; '%self.length))

    def make_target(self, text):
        voc = list(self.classes.keys())
        return np.array([self.classes[char] if char in voc else self.classes['-'] for char in text])

    def parse_name(self, name):
        return name.split('.')[-2].split('/')[-1]

    def get_labels(self, names):
        Y_data = np.full([len(names), self.max_len], self.blank)
        c = 0
        for i, name in enumerate(names):
            if self.mjsynth:
                try:
                    img, word = self.open_img(name, pad=True)
                except:
                    continue
            else:
                word = self.targets[self.parse_name(name)]
            word = word.lower()
            c += 1
            Y_data[i, 0:len(word)] = self.make_target(word)
        return Y_data

    def open_img(self, name, pad=True):
        try:
            name = re.sub(" ", "_", name.split()[0])[1:]
        except:
            name = name[1:-1]
        img = cv2.imread(self.path+name)
        img = np.array(img, dtype=np.uint8)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if self.flag:
            img_size = self.img_size
        else:
            img_size = (self.img_size[1], self.img_size[0])
        if pad and img.shape[1] < img_size[1]:
            val, counts = np.unique(img, return_counts=True)
            to_fill = val[np.where(counts == counts.max())[0][0]]
            img = np.concatenate([img, np.full((img.shape[0], img_size[1]-img.shape[1]), to_fill)], axis=1)
        img = cv2.resize(img, (img_size[1], img_size[0]), Image.LANCZOS)
        return img, name.split("_")[-2]

    def get_blank_matrices(self):
        shape = [self.batch_size, self.img_size[1], self.img_size[0], 1]
        if not self.flag:
            shape[1], shape[2] = shape[2], shape[1]

        X_data = np.empty(shape)
        Y_data = np.full([self.batch_size, self.max_len], self.blank)
        input_length = np.ones((self.batch_size, 1))
        label_length = np.zeros((self.batch_size, 1))
        return X_data, Y_data, input_length, label_length

    def run_generator(self, names, downsample_factor):
        source_str, i, n = [], 0, 0
        N = len(names) // self.batch_size
        X_data, Y_data, input_length, label_length = self.get_blank_matrices()
        if self.mjsynth:
            while True:
                for name in names:
                    try:
                        img, word = self.open_img(name, pad=True)
                    except:
                        continue

                    word = word.lower()
                    source_str.append(word)

                    word = self.make_target(word)
                    Y_data[i, 0:len(word)] = word
                    label_length[i] = len(word)

                    if self.normed:
                        img = (img - self.mean) / self.std

                    if img.shape[1] >= img.shape[0]:
                        img = img[::-1].T
                        if not self.flag:
                            self.img_size = (self.img_size[1], self.img_size[0])
                            self.flag = True

                    if self.flag:
                        input_length[i] = (self.img_size[1]+4) // downsample_factor - 2
                    else:
                        input_length[i] = (self.img_size[0]+4) // downsample_factor - 2

                    img = img[:,:,np.newaxis]

                    X_data[i] = img
                    i += 1

                    if n == N and i == (len(names) % self.batch_size):
                        inputs = {
                            'the_input': X_data,
                            'the_labels': Y_data,
                            'input_length': input_length,
                            'label_length': label_length,
                            'source_str': np.array(source_str)
                        }
                        outputs = {'ctc': np.zeros([self.batch_size])}
                        yield (inputs, outputs)

                    elif i == self.batch_size:
                        inputs = {
                            'the_input': X_data,
                            'the_labels': Y_data,
                            'input_length': input_length,
                            'label_length': label_length,
                            'source_str': np.array(source_str)
                        }
                        outputs = {'ctc': np.zeros([self.batch_size])}
                        source_str, i = [], 0
                        X_data, Y_data, input_length, label_length = self.get_blank_matrices()
                        n += 1
                        yield (inputs, outputs)


        else:
            while True:
                for name in names:

                    img = cv2.imread(name)
                    img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

                    #no need for now, because IAM dataset already preprocessed with all needed offsets
                    #img = set_offset_monochrome(img, offset=self.offset, fill=self.fill)

                    word = self.parse_name(name)
                    source_str.append(word)
                    word = self.targets[word]

                    Y_data[i, 0:len(word)] = word
                    label_length[i] = len(word)

                    #invert image colors.
                    img = cv2.bitwise_not(img)

                    if img.shape[1] >= img.shape[0]:
                        img = img[::-1].T
                        if not self.flag:
                            self.img_size = (self.img_size[1], self.img_size[0])
                            self.flag = True

                    if self.flag:
                        input_length[i] = (self.img_size[1]+4) // downsample_factor - 2
                    else:
                        input_length[i] = (self.img_size[0]+4) // downsample_factor - 2

                    img = cv2.resize(img, self.img_size, Image.LANCZOS)
                    img = cv2.threshold(img, self.trsh, 255,
                        cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]

                    img = img[:,:,np.newaxis]

                    if self.normed:
                        img = norm(img)

                    X_data[i] = img
                    i += 1

                    if n == N and i == (len(names) % self.batch_size):
                        inputs = {
                            'the_input': X_data,
                            'the_labels': Y_data,
                            'input_length': input_length,
                            'label_length': label_length,
                            'source_str': np.array(source_str)
                        }
                        outputs = {'ctc': np.zeros([self.batch_size])}
                        yield (inputs, outputs)

                    elif i == self.batch_size:
                        inputs = {
                            'the_input': X_data,
                            'the_labels': Y_data,
                            'input_length': input_length,
                            'label_length': label_length,
                            'source_str': np.array(source_str)
                        }
                        outputs = {'ctc': np.zeros([self.batch_size])}
                        source_str, i = [], 0
                        X_data, Y_data, input_length, label_length = self.get_blank_matrices()
                        n += 1
                        yield (inputs, outputs)

def get_lengths(names):
    d = {}
    for name in tqdm(names, desc="getting words lengths"):
        try:
            edited_name = re.sub(" ", "_", name.split()[0])[1:]
        except:
            edited_name = edited_name[1:-1]
        d[name] = len(edited_name.split("_")[-2])
    return d

def norm(image):
    return image.astype('float32') / 255.

def dist(a, b):
    return np.power((np.power((a[0] - b[0]), 2) + np.power((a[1] - b[1]), 2)), 1./2)

def coords2img(coords, dotSize=4, img_size=(64,64), offset=20):

    def min_max(coords):
        max_x, min_x = int(np.max(np.concatenate([coord[:, 0] for coord in coords]))), int(np.min(np.concatenate([coord[:, 0] for coord in coords]))) 
        max_y, min_y = int(np.max(np.concatenate([coord[:, 1] for coord in coords]))), int(np.min(np.concatenate([coord[:, 1] for coord in coords])))
        return min_x, max_x, min_y, max_y
    
    offset += dotSize // 2
    min_dists, dists = {}, [[] for i in range(len(coords))]
    for i, line in enumerate(coords):
        for point in line:
            dists[i].append(dist([0, 0], point))
        min_dists[min(dists[i])] = i
            
    min_dist = min(list(min_dists.keys()))
    min_index = min_dists[min_dist]
    start_point = coords[min_index][dists[min_index].index(min_dist)].copy()
    for i in range(len(coords)):
        coords[i] -= start_point
    
    min_x, max_x, min_y, max_y = min_max(coords) 
    scaleX = ((max_x - min_x) / (img_size[0]-(offset*2-1)))
    scaleY = ((max_y - min_y) / (img_size[1]-(offset*2-1)))
    for line in coords:
        line[:, 0] = line[:, 0] / scaleX
        line[:, 1] = line[:, 1] / scaleY

    min_x, max_x, min_y, max_y = min_max(coords)
        
    w = max_x-min_x+offset*2
    h = max_y-min_y+offset*2

    img = Image.new("RGB", (w, h), "white")
    draw = ImageDraw.Draw(img)

    start = 1
    for i in range(len(coords)):
        for j in range(len(coords[i]))[start:]:
            x, y = coords[i][j-1]
            x_n, y_n = coords[i][j]
            x -= min_x-offset; y -= min_y-offset
            x_n -= min_x-offset; y_n -= min_y-offset
            draw.line([(x,y), (x_n,y_n)], fill="black", width=dotSize)

    return {'img':img, 'scaleX':scaleX, 'scaleY':scaleY, 'start_point': start_point}

def padd(img, length_bins=[], height_bins=[], pad=True, left_offset=10):
    for axis, bins in zip([1, 0], [length_bins, height_bins]):
        if bins is not None:
            if type(bins) == int and img.shape[axis] < bins:
                if pad:
                    if axis == 1:
                        delta = bins-img.shape[1]-left_offset
                        if delta <= 0:
                            continue
                        img = np.concatenate([img, np.full((img.shape[0], delta, img.shape[-1]), 255)], axis=1)
                        img = np.concatenate([img, np.full((img.shape[0], left_offset, img.shape[-1]), 255)], axis=1)
                    else:
                        delta = bins-img.shape[0]
                        if (delta//2-1) <= 0:
                            continue
                        img = np.concatenate([np.full((delta//2-1, img.shape[1], img.shape[-1]), 255), img], axis=0)
                        img = np.concatenate([img, np.full((delta-(delta//2-1), img.shape[1], img.shape[-1]), 255)], axis=0)
                else:
                    if axis == 1:
                        img = cv2.resize(img, (bins, img.shape[0]), Image.LANCZOS)
                    else:
                        img = cv2.resize(img, (img.shape[1], bins), Image.LANCZOS)
            elif type(bins) == list:
                length = img.shape[axis]
                for j in range(len(bins)-1):
                    if bins[j] < img.shape[axis] <= ((bins[j+1] - bins[j])/2+bins[j]):
                        length = bins[j]
                    elif bins[j+1] == bins[-1] and bins[j+1] < img.shape[axis]:
                        length = bins[j+1]
                    elif ((bins[j+1] - bins[j])/2+bins[j]) < img.shape[axis] <= bins[j+1]:
                        length = bins[j+1]
                if pad:
                    if axis == 1:
                        delta = length-img.shape[1]-left_offset
                        if delta <= 0:
                            continue
                        img = np.concatenate([img, np.full((img.shape[0], delta, img.shape[-1]), 255)], axis=1)
                        img = np.concatenate([img, np.full((img.shape[0], left_offset, img.shape[-1]), 255)], axis=1)
                    else:
                        delta = length-img.shape[0]
                        if (delta//2-1) <= 0:
                            continue
                        img = np.concatenate([np.full((delta//2-1, img.shape[1], img.shape[-1]), 255), img], axis=0)
                        img = np.concatenate([img, np.full((delta-(delta//2-1), img.shape[1], img.shape[-1]), 255)], axis=0)
                else:
                    if axis == 1:
                        img = cv2.resize(img, (length, img.shape[0]), Image.LANCZOS)
                    else:
                        img = cv2.resize(img, (img.shape[1], length), Image.LANCZOS)

            elif img.shape[axis] != bins or img.shape[axis] > bins:
                continue
    return img

def bbox2(img):
    rows = np.any(img, axis=1)
    cols = np.any(img, axis=0)
    rmin, rmax = np.where(rows)[0][[0, -1]]
    cmin, cmax = np.where(cols)[0][[0, -1]]

    rmin = (np.where(img[rmin])[0][0], rmin)
    rmax = (np.where(img[rmax])[0][0], rmax)
    
    cmin = (cmin, np.where(img[:, cmin])[0][0])
    cmax = (cmax, np.where(img[:, cmax])[0][0])
    
    return np.array((rmin, rmax, cmin, cmax))

def set_offset_monochrome(img, offset=20, fill=0):

    shape = np.array(img.shape)
    if img.ndim > 2:
        box = bbox2(img.sum(axis=2))
    else:
        box = bbox2(img)
    box_y_min, box_y_max = np.min(box[:, 1]), np.max(box[:, 1])
    box_x_min, box_x_max = np.min(box[:, 0]), np.max(box[:, 0])
    
    y1, y2 = box_y_min - offset, box_y_max + offset
    x1, x2 = box_x_min - offset, box_x_max + offset
    
    if x1 < 0:
        x1 *= -1
        if img.ndim > 2:
            img = np.concatenate([np.full((img.shape[0], x1, img.shape[-1]), fill), img], axis=1)
        else:
            img = np.concatenate([np.full((img.shape[0], x1), fill), img], axis=1)
        x1 = 0
    if x2 > shape[1]:
        if img.ndim > 2:
            img = np.concatenate([img, np.full((img.shape[0], x2-shape[1], img.shape[-1]), fill)], axis=1)
        else:
            img = np.concatenate([img, np.full((img.shape[0], x2-shape[1]), fill)], axis=1)
        x2 = -1
    if y1 < 0:
        y1 *= -1
        if img.ndim > 2:
            img = np.concatenate([np.full((y1, img.shape[1], img.shape[-1]), fill), img], axis=0)
        else:
            img = np.concatenate([np.full((y1, img.shape[1]), fill), img], axis=0)
        y1 = 0
    if y2 > shape[0]:
        if img.ndim > 2:
            img = np.concatenate([img, np.full((y2-shape[0], img.shape[1], img.shape[-1]), fill)], axis=0)
        else:
            img = np.concatenate([img, np.full((y2-shape[0], img.shape[1]), fill)], axis=0)
        y2 = -1
        
    return img[y1:y2, x1:x2].astype(np.uint8)

def save_single_model(path):
    sinlge_model = multi_model.layers[-2]
    sinlge_model.save(path+'/single_gpu_model.hdf5')
    model_json = sinlge_model.to_json()
    with open(path+"/single_gpu_model.json", "w") as json_file:
        json_file.write(model_json)
    sinlge_model.save_weights(path+"/single_gpu_weights.h5")

#####################################################################################################################
# NOT MINE CODE:
#####################################################################################################################

from tensorflow.python.client import device_lib
def get_available_gpus():
    local_device_protos = device_lib.list_local_devices()
    return [x.name for x in local_device_protos if x.device_type == 'GPU']

class LossHistory(Callback):
    def on_train_begin(self, logs={}):
        self.losses = {"loss":[], "val_loss":[]}

    def on_batch_end(self, batch, logs={}):
        self.losses["loss"].append(logs.get('loss'))
        self.losses["val_loss"].append(logs.get('val_loss'))

##############################################################################################################
# CLR port from fastai

class LR_Updater(Callback):
    '''This callback is utilized to log learning rates every iteration (batch cycle)
    it is not meant to be directly used as a callback but extended by other callbacks
    ie. LR_Cycle
    '''
    def __init__(self, iterations, epochs=1):
        '''
        iterations = dataset size / batch size
        epochs = pass through full training dataset
        '''
        self.epoch_iterations = iterations
        self.trn_iterations = 0.
        self.history = {}
    def setRate(self):
        return K.get_value(self.model.optimizer.lr)
    def on_train_begin(self, logs={}):
        self.trn_iterations = 0.
        logs = logs or {}
    def on_batch_end(self, batch, logs=None):
        logs = logs or {}
        self.trn_iterations += 1
        K.set_value(self.model.optimizer.lr, self.setRate())
        self.history.setdefault('lr', []).append(K.get_value(self.model.optimizer.lr))
        self.history.setdefault('iterations', []).append(self.trn_iterations)
        for k, v in logs.items():
            self.history.setdefault(k, []).append(v)
    def plot_lr(self):
        plt.xlabel("iterations")
        plt.ylabel("learning rate")
        plt.plot(self.history['iterations'], self.history['lr'])
    def plot(self, n_skip=10):
        plt.xlabel("learning rate (log scale)")
        plt.ylabel("loss")
        plt.plot(self.history['lr'][n_skip:-5], self.history['loss'][n_skip:-5])
        plt.xscale('log')

class LR_Find(LR_Updater):
    '''This callback is utilized to determine the optimal lr to be used
    it is based on this pytorch implementation https://github.com/fastai/fastai/blob/master/fastai/learner.py
    and adopted from this keras implementation https://github.com/bckenstler/CLR
    it loosely implements methods described in the paper https://arxiv.org/pdf/1506.01186.pdf
    '''

    def __init__(self, iterations, epochs=1, min_lr=1e-05, max_lr=10, jump=6):
        '''
        iterations = dataset size / batch size
        epochs should always be 1
        min_lr is the starting learning rate
        max_lr is the upper bound of the learning rate
        jump is the x-fold loss increase that will cause training to stop (defaults to 6)
        '''
        self.min_lr = min_lr
        self.max_lr = max_lr
        self.lr_mult = (max_lr/min_lr)**(1/iterations)
        self.jump = jump
        super().__init__(iterations, epochs=epochs)
    def setRate(self):
        return self.min_lr * (self.lr_mult**self.trn_iterations)
    def on_train_begin(self, logs={}):
        super().on_train_begin(logs=logs)
        try: #multiple lr's
            K.get_variable_shape(self.model.optimizer.lr)[0]
            self.min_lr = np.full(K.get_variable_shape(self.model.optimizer.lr),self.min_lr)
        except IndexError:
            pass
        K.set_value(self.model.optimizer.lr, self.min_lr)
        self.best=1e9
        self.model.save_weights('tmp.hd5') #save weights
    def on_train_end(self, logs=None):
        self.model.load_weights('tmp.hd5') #load_weights
    def on_batch_end(self, batch, logs=None):
        #check if we have made an x-fold jump in loss and training should stop
        try:
            loss = self.history['loss'][-1]
            if math.isnan(loss) or loss > self.best*self.jump:
                self.model.stop_training = True
            if loss < self.best:
                self.best=loss
        except KeyError:
            pass
        super().on_batch_end(batch, logs=logs)
        
class LR_Cycle(LR_Updater):
    '''This callback is utilized to implement cyclical learning rates
    it is based on this pytorch implementation https://github.com/fastai/fastai/blob/master/fastai
    and adopted from this keras implementation https://github.com/bckenstler/CLR
    it loosely implements methods described in the paper https://arxiv.org/pdf/1506.01186.pdf
    '''
    def __init__(self, iterations, cycle_len=1, cycle_mult=1, epochs=1):
        '''
        iterations = dataset size / batch size
        epochs #todo do i need this or can it accessed through self.model
        cycle_len = num of times learning rate anneals from its max to its min in an epoch
        cycle_mult = used to increase the cycle length cycle_mult times after every cycle
        for example: cycle_mult = 2 doubles the length of the cycle at the end of each cy$
        '''
        self.min_lr = 0
        self.cycle_len = cycle_len
        self.cycle_mult = cycle_mult
        self.cycle_iterations = 0.
        super().__init__(iterations, epochs=epochs)
    def setRate(self):
        self.cycle_iterations += 1
        cos_out = np.cos(np.pi*(self.cycle_iterations)/self.epoch_iterations) + 1
        if self.cycle_iterations==self.epoch_iterations:
            self.cycle_iterations = 0.
            self.epoch_iterations *= self.cycle_mult
        return self.max_lr / 2 * cos_out
    def on_train_begin(self, logs={}):
        super().on_train_begin(logs={}) #changed to {} to fix plots after going from 1 to mult. lr
        self.cycle_iterations = 0.
        self.max_lr = K.get_value(self.model.optimizer.lr)

##############################################################################################################

class CyclicLR(Callback):
    """This callback implements a cyclical learning rate policy (CLR).
    The method cycles the learning rate between two boundaries with
    some constant frequency, as detailed in this paper (https://arxiv.org/abs/1506.01186).
    The amplitude of the cycle can be scaled on a per-iteration or 
    per-cycle basis.
    This class has three built-in policies, as put forth in the paper.
    "triangular":
        A basic triangular cycle w/ no amplitude scaling.
    "triangular2":
        A basic triangular cycle that scales initial amplitude by half each cycle.
    "exp_range":
        A cycle that scales initial amplitude by gamma**(cycle iterations) at each 
        cycle iteration.
    For more detail, please see paper.
    
    # Example
        ```python
            clr = CyclicLR(base_lr=0.001, max_lr=0.006,
                                step_size=2000., mode='triangular')
            model.fit(X_train, Y_train, callbacks=[clr])
        ```
    
    Class also supports custom scaling functions:
        ```python
            clr_fn = lambda x: 0.5*(1+np.sin(x*np.pi/2.))
            clr = CyclicLR(base_lr=0.001, max_lr=0.006,
                                step_size=2000., scale_fn=clr_fn,
                                scale_mode='cycle')
            model.fit(X_train, Y_train, callbacks=[clr])
        ```    
    # Arguments
        base_lr: initial learning rate which is the
            lower boundary in the cycle.
        max_lr: upper boundary in the cycle. Functionally,
            it defines the cycle amplitude (max_lr - base_lr).
            The lr at any cycle is the sum of base_lr
            and some scaling of the amplitude; therefore 
            max_lr may not actually be reached depending on
            scaling function.
        step_size: number of training iterations per
            half cycle. Authors suggest setting step_size
            2-8 x training iterations in epoch.
        mode: one of {triangular, triangular2, exp_range}.
            Default 'triangular'.
            Values correspond to policies detailed above.
            If scale_fn is not None, this argument is ignored.
        gamma: constant in 'exp_range' scaling function:
            gamma**(cycle iterations)
        scale_fn: Custom scaling policy defined by a single
            argument lambda function, where 
            0 <= scale_fn(x) <= 1 for all x >= 0.
            mode paramater is ignored 
        scale_mode: {'cycle', 'iterations'}.
            Defines whether scale_fn is evaluated on 
            cycle number or cycle iterations (training
            iterations since start of cycle). Default is 'cycle'.
    """

    def __init__(self, base_lr=0.001, max_lr=0.006, step_size=2000., mode='triangular',
                 gamma=1., scale_fn=None, scale_mode='cycle'):
        super(CyclicLR, self).__init__()

        self.base_lr = base_lr
        self.max_lr = max_lr
        self.step_size = step_size
        self.mode = mode
        self.gamma = gamma
        if scale_fn == None:
            if self.mode == 'triangular':
                self.scale_fn = lambda x: 1.
                self.scale_mode = 'cycle'
            elif self.mode == 'triangular2':
                self.scale_fn = lambda x: 1/(2.**(x-1))
                self.scale_mode = 'cycle'
            elif self.mode == 'exp_range':
                self.scale_fn = lambda x: gamma**(x)
                self.scale_mode = 'iterations'
        else:
            self.scale_fn = scale_fn
            self.scale_mode = scale_mode
        self.clr_iterations = 0.
        self.trn_iterations = 0.
        self.history = {}

        self._reset()

    def _reset(self, new_base_lr=None, new_max_lr=None,
               new_step_size=None):
        """Resets cycle iterations.
        Optional boundary/step size adjustment.
        """
        if new_base_lr != None:
            self.base_lr = new_base_lr
        if new_max_lr != None:
            self.max_lr = new_max_lr
        if new_step_size != None:
            self.step_size = new_step_size
        self.clr_iterations = 0.
        
    def clr(self):
        cycle = np.floor(1+self.clr_iterations/(2*self.step_size))
        x = np.abs(self.clr_iterations/self.step_size - 2*cycle + 1)
        if self.scale_mode == 'cycle':
            return self.base_lr + (self.max_lr-self.base_lr)*np.maximum(0, (1-x))*self.scale_fn(cycle)
        else:
            return self.base_lr + (self.max_lr-self.base_lr)*np.maximum(0, (1-x))*self.scale_fn(self.clr_iterations)
        
    def on_train_begin(self, logs={}):
        logs = logs or {}

        if self.clr_iterations == 0:
            K.set_value(self.model.optimizer.lr, self.base_lr)
        else:
            K.set_value(self.model.optimizer.lr, self.clr())        
            
    def on_batch_end(self, epoch, logs=None):
        
        logs = logs or {}
        self.trn_iterations += 1
        self.clr_iterations += 1

        self.history.setdefault('lr', []).append(K.get_value(self.model.optimizer.lr))
        self.history.setdefault('iterations', []).append(self.trn_iterations)

        for k, v in logs.items():
            self.history.setdefault(k, []).append(v)
        
        K.set_value(self.model.optimizer.lr, self.clr())

#ATTENTION LAYER
def get_activations(model, inputs, print_shape_only=False, layer_name=None):
    # Documentation is available online on Github at the address below.
    # From: https://github.com/philipperemy/keras-visualize-activations
    print('----- activations -----')
    activations = []
    inp = model.input
    if layer_name is None:
        outputs = [layer.output for layer in model.layers]
    else:
        outputs = [layer.output for layer in model.layers if layer.name == layer_name]  # all layer outputs
    funcs = [K.function([inp] + [K.learning_phase()], [out]) for out in outputs]  # evaluation functions
    layer_outputs = [func([inputs, 1.])[0] for func in funcs]
    for layer_activations in layer_outputs:
        activations.append(layer_activations)
        if print_shape_only:
            print(layer_activations.shape)
        else:
            print(layer_activations)
    return activations


def get_data(n, input_dim, attention_column=1):
    """
    Data generation. x is purely random except that it's first value equals the target y.
    In practice, the network should learn that the target = x[attention_column].
    Therefore, most of its attention should be focused on the value addressed by attention_column.
    :param n: the number of samples to retrieve.
    :param input_dim: the number of dimensions of each element in the series.
    :param attention_column: the column linked to the target. Everything else is purely random.
    :return: x: model inputs, y: model targets
    """
    x = np.random.standard_normal(size=(n, input_dim))
    y = np.random.randint(low=0, high=2, size=(n, 1))
    x[:, attention_column] = y[:, 0]
    return x, y


def get_data_recurrent(n, time_steps, input_dim, attention_column=10):
    """
    Data generation. x is purely random except that it's first value equals the target y.
    In practice, the network should learn that the target = x[attention_column].
    Therefore, most of its attention should be focused on the value addressed by attention_column.
    :param n: the number of samples to retrieve.
    :param time_steps: the number of time steps of your series.
    :param input_dim: the number of dimensions of each element in the series.
    :param attention_column: the column linked to the target. Everything else is purely random.
    :return: x: model inputs, y: model targets
    """
    x = np.random.standard_normal(size=(n, time_steps, input_dim))
    y = np.random.randint(low=0, high=2, size=(n, 1))
    x[:, attention_column, :] = np.tile(y[:], (1, input_dim))
    return x, y

def attention_3d_block(inputs, time_steps, single_attention_vector):
    # inputs.shape = (batch_size, time_steps, input_dim)
    input_dim = int(inputs.shape[2])
    a = Permute((2, 1))(inputs)
    a = Reshape((input_dim, time_steps))(a) # this line is not useful. It's just to know which dimension is what.
    a = Dense(time_steps, activation='softmax')(a)
    if single_attention_vector:
        a = Lambda(lambda x: K.mean(x, axis=1), name='dim_reduction')(a)
        a = RepeatVector(input_dim)(a)
    a_probs = Permute((2, 1), name='attention_vec')(a)
    #output_attention_mul = merge([inputs, a_probs], name='attention_mul', mode='mul') #merge is depricated
    output_attention_mul = multiply([inputs, a_probs], name='attention_mul')
    return output_attention_mul

# def model_attention_applied_after_lstm():
#     inputs = Input(shape=(TIME_STEPS, INPUT_DIM,))
#     lstm_units = 32
#     lstm_out = LSTM(lstm_units, return_sequences=True)(inputs)
#     attention_mul = attention_3d_block(lstm_out)
#     attention_mul = Flatten()(attention_mul)
#     output = Dense(1, activation='sigmoid')(attention_mul)
#     model = Model(input=[inputs], output=output)
#     return model

# def model_attention_applied_before_lstm():
#     inputs = Input(shape=(TIME_STEPS, INPUT_DIM,))
#     attention_mul = attention_3d_block(inputs)
#     lstm_units = 32
#     attention_mul = LSTM(lstm_units, return_sequences=False)(attention_mul)
#     output = Dense(1, activation='sigmoid')(attention_mul)
#     model = Model(input=[inputs], output=output)
#     return models