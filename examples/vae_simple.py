import sys
import os
import numpy as np
import theano
import theano.tensor as T
import lasagne as nn
import time
import pickle
from PIL import Image
from lasagne.layers import MergeLayer
from theano.tensor.shared_randomstreams import RandomStreams
from scipy.stats import norm

# ############################################################################
# Tencia Lee
# Some code borrowed from:
# https://github.com/Lasagne/Lasagne/blob/master/examples/mnist.py
#
# Implementation of variational autoencoder (AEVB) algorithm as in:
# [1] arXiv:1312.6114 [stat.ML] (Diederik P Kingma, Max Welling 2013)

# ################## Download and prepare the MNIST dataset ##################
# For the linked MNIST data, the autoencoder learns well only in binary mode.
# This is most likely due to the distribution of the values. Most pixels are
# either very close to 0, or very close to 1.

def load_dataset():
    if sys.version_info[0] == 2:
        from urllib import urlretrieve
    else:
        from urllib.request import urlretrieve

    def download(filename, source='http://yann.lecun.com/exdb/mnist/'):
        print("Downloading %s" % filename)
        urlretrieve(source + filename, filename)

    import gzip
    def load_mnist_images(filename):
        if not os.path.exists(filename):
            download(filename)
        with gzip.open(filename, 'rb') as f:
            data = np.frombuffer(f.read(), np.uint8, offset=16)
        data = data.reshape(-1, 1, 28, 28).transpose(0,1,3,2)
        return data / np.float32(255)

    X_train = load_mnist_images('train-images-idx3-ubyte.gz')
    X_test = load_mnist_images('t10k-images-idx3-ubyte.gz')
    X_train, X_val = X_train[:-10000], X_train[-10000:]
    return X_train, X_val, X_test

# ############################# Output images ################################
# image processing using PIL

def get_image_array(X, index, shp=(28,28), channels=1):
    ret = (X[index] * 255.).reshape(channels,shp[0],shp[1]) \
            .transpose(2,1,0).clip(0,255).astype(np.uint8)
    if channels == 1:
        ret = ret.reshape(shp[1], shp[0])
    return ret

def get_image_pair(X, Xpr, channels=1, idx=-1):
    mode = 'RGB' if channels == 3 else 'L'
    shp=X[0][0].shape
    i = np.random.randint(X.shape[0]) if idx == -1 else idx
    orig = Image.fromarray(get_image_array(X, i, shp, channels), mode=mode)
    ret = Image.new(mode, (orig.size[0], orig.size[1]*2))
    ret.paste(orig, (0,0))
    new = Image.fromarray(get_image_array(Xpr, i, shp, channels), mode=mode)
    ret.paste(new, (0, orig.size[1]))
    return ret

# ############################# Batch iterator ###############################

def iterate_minibatches(inputs, targets, batchsize, shuffle=False):
    assert len(inputs) == len(targets)
    if shuffle:
        indices = np.arange(len(inputs))
        np.random.shuffle(indices)
    for start_idx in range(0, len(inputs) - batchsize + 1, batchsize):
        if shuffle:
            excerpt = indices[start_idx:start_idx + batchsize]
        else:
            excerpt = slice(start_idx, start_idx + batchsize)
        yield inputs[excerpt], targets[excerpt]


# ##################### Custom layer for middle of VCAE ######################
# This layer takes the mu and sigma (both DenseLayers) and combines them with
# a random vector epsilon to sample values for a multivariate Gaussian

class GaussianSampleLayer(MergeLayer):
    def __init__(self, mu, logsigma, rng=None, **kwargs):
        self.rng = rng if rng else RandomStreams(nn.random.get_rng().randint(1,2147462579))
        super(GaussianSampleLayer, self).__init__([mu, logsigma], **kwargs)

    def get_output_shape_for(self, input_shapes):
        return input_shapes[0]

    def get_output_for(self, inputs, **kwargs):
        mu, logsigma = inputs
        shape=(self.input_shapes[0][0] or inputs[0].shape[0],
                self.input_shapes[0][1] or inputs[0].shape[1])
        return mu + T.exp(logsigma) * self.rng.normal(shape)


# ############################## Build Model #################################
# encoder has 1 hidden layer, where we get mu and sigma for Z given an inp X
# continuous decoder has 1 hidden layer, where we get mu and sigma for X given code Z
# binary decoder has 1 hidden layer, where we calculate p(X=1)
# once we have (mu, sigma) for Z, we sample L times
# Then L separate outputs are constructed and the final layer averages them

def build_vae(inputvar, L=1, binary=False, imgshape=(28,28), channels=1, z_dim=2, n_hid=256):
    x_dim = imgshape[0] * imgshape[1] * channels
    l_input = nn.layers.InputLayer(shape=(None,channels,imgshape[0], imgshape[1]),
            input_var=inputvar, name='input')
    l_enc_hid = nn.layers.DenseLayer(l_input, num_units=n_hid,
            nonlinearity=T.nnet.softplus, name='enc_hid')
    l_enc_mu = nn.layers.DenseLayer(l_enc_hid, num_units=z_dim,
            nonlinearity = None, name='enc_mu')
    l_enc_logsigma = nn.layers.DenseLayer(l_enc_hid, num_units=z_dim,
            nonlinearity = None, name='enc_logsigma')
    l_Z_list = []
    l_dec_mu_list = []
    l_dec_logsigma_list = []
    l_output_list = []
    # tie the weights of all L versions so they are the "same" layer
    W_dec_hid = None
    b_dec_hid = None
    W_dec_mu = None
    b_dec_mu = None
    W_dec_ls = None
    b_dec_ls = None
    for i in xrange(L):
        l_Z = GaussianSampleLayer(l_enc_mu, l_enc_logsigma, name='Z')
        l_dec_hid = nn.layers.DenseLayer(l_Z, num_units=n_hid,
                nonlinearity = T.nnet.softplus,
                W=nn.init.GlorotUniform() if W_dec_hid is None else W_dec_hid,
                b=nn.init.Constant(0.) if b_dec_hid is None else b_dec_hid,
                name='dec_hid')
        if binary:
            l_output = nn.layers.DenseLayer(l_dec_hid, num_units = x_dim,
                    nonlinearity = nn.nonlinearities.sigmoid,
                    W = nn.init.GlorotUniform() if W_dec_mu is None else W_dec_mu,
                    b = nn.init.Constant(0.) if b_dec_mu is None else b_dec_mu,
                    name = 'dec_output')
            l_output_list.append(l_output)
            if W_dec_hid is None:
                W_dec_hid = l_dec_hid.W
                b_dec_hid = l_dec_hid.b
                W_dec_mu = l_output.W
                b_dec_mu = l_output.b
        else:
            l_dec_mu = nn.layers.DenseLayer(l_dec_hid, num_units=x_dim,
                    nonlinearity = None,
                    W = nn.init.GlorotUniform() if W_dec_mu is None else W_dec_mu,
                    b = nn.init.Constant(0) if b_dec_mu is None else b_dec_mu,
                    name = 'dec_mu')
            # relu_shift is for numerical stability - if training data has any
            # dimensions where stdev=0, allowing logsigma to approach -inf
            # will cause the loss function to become NAN. So we set the limit
            # stdev >= exp(-1 * relu_shift)
            relu_shift = 10
            l_dec_logsigma = nn.layers.DenseLayer(l_dec_hid, num_units=x_dim,
                    W = nn.init.GlorotUniform() if W_dec_ls is None else W_dec_ls,
                    b = nn.init.Constant(0) if b_dec_ls is None else b_dec_ls,
                    nonlinearity = lambda a: T.nnet.relu(a+relu_shift)-relu_shift,
                    name='dec_logsigma')
            l_output = GaussianSampleLayer(l_dec_mu, l_dec_logsigma,
                    name='dec_output')
            l_dec_mu_list.append(l_dec_mu)
            l_dec_logsigma_list.append(l_dec_logsigma)
            l_output_list.append(l_output)
            if W_dec_hid is None:
                W_dec_hid = l_dec_hid.W
                b_dec_hid = l_dec_hid.b
                W_dec_mu = l_dec_mu.W
                b_dec_mu = l_dec_mu.b
                W_dec_ls = l_dec_logsigma.W
                b_dec_ls = l_dec_logsigma.b
        l_Z_list.append(l_Z)
    l_output = nn.layers.ElemwiseSumLayer(l_output_list, coeffs=1./L, name='output')
    return l_enc_mu, l_enc_logsigma, l_dec_mu_list, l_dec_logsigma_list, l_output_list, l_output

# ############################## Main program ################################

# helper function for log-likelihood expression
def log_likelihood(tgt, mu, ls):
    return T.sum(-(np.float32(0.5 * np.log(2 * np.pi)) + ls)
            - 0.5 * T.sqr(tgt - mu) / T.exp(2 * ls))

def main(L=2, z_dim=2, n_hid=256, num_epochs=20, binary=True):
    print("Loading data...")
    X_train, X_val, X_test = load_dataset()
    X_train_tgt = X_train.reshape(X_train.shape[0],-1)
    X_val_tgt = X_val.reshape(X_val.shape[0],-1)
    X_test_tgt = X_test.reshape(X_test.shape[0],-1)
    width, height = X_train.shape[2], X_train.shape[3]
    input_var = T.tensor4('inputs')
    target_var = T.dmatrix('targets')

    # Create VAE model
    print("Building model and compiling functions...")
    print("L = {}, z_dim = {}, n_hid = {}, binary={}".format(L, z_dim, n_hid, binary))
    x_dim = width * height
    l_z_mu, l_z_ls, l_x_mu_list, l_x_ls_list, l_x_list, l_x = \
           build_vae(input_var, L=L, binary=binary, z_dim=z_dim, n_hid=n_hid)

    # If there are dropout layers etc these functions return masked or non-masked expressions
    # depending on if they will be used for training or validation/test err calcs
    z_mu = lambda b: nn.layers.get_output(l_z_mu, deterministic=b)
    z_ls = lambda b: nn.layers.get_output(l_z_ls, deterministic=b)
    x_mu = lambda b: [nn.layers.get_output(l_x_mu, deterministic=b) for l_x_mu in l_x_mu_list]
    x_ls = lambda b: [nn.layers.get_output(l_x_ls, deterministic=b) for l_x_ls in l_x_ls_list]
    x_list = lambda b: [nn.layers.get_output(l_x1, deterministic=b) for l_x1 in l_x_list]

    # Loss expression has two parts as specified in [1]
    # kl_div = KL divergence between p_theta(z) and p(z|x)
    # - divergence between prior distr and approx posterior of z given x
    # - or how likely we are to see this z when accounting for Gaussian prior
    # logpxz = log p(x|z)
    # - log-likelihood of x given z
    # - in binary case logpxz = cross-entropy
    # - in continuous case, is log-likelihood of seeing the target x under the
    #   Gaussian distribution parameterized by dec_mu, sigma = exp(dec_logsigma)
    kl_div = lambda b: 0.5 * T.sum(1 + 2*z_ls(b) - T.sqr(z_mu(b)) - T.exp(2 * z_ls(b)))
    if binary:
        logpxz = lambda b: T.sum([nn.objectives.binary_crossentropy(x, target_var).sum()
            for x in x_list(b)]) * (-1./L)
        test_prediction = nn.layers.get_output(l_x, deterministic=True)
    else:
        logpxz = lambda b: T.sum([log_likelihood(target_var, mu, ls)
            for mu, ls in zip(x_mu(b), x_ls(b))])/L
        test_prediction = T.sum(x_mu(True), axis=0)/L
    loss = -1 * (logpxz(False) + kl_div(False))
    test_loss = -1 * (logpxz(True) + kl_div(True))

    # functions for generating images given a code (used for visualization)
    # we give the function a certain z and then set logsigma(z) to a very negative
    # value to make the sampling nearly deterministic
    z_var = T.dvector()
    x_bin = nn.layers.get_output(l_x, {l_z_mu:z_var, l_z_ls:[-100,-100]},
            deterministic=True)
    x_cont = T.sum([nn.layers.get_output(l_x_mu, {l_z_mu:z_var, l_z_ls:[-100,-100]},
            determistic=True) for l_x_mu in l_x_mu_list], axis=0) / L
    gen_fn = theano.function([z_var], x_bin if binary else x_cont)
        
    # ADAM updates
    params = nn.layers.get_all_params(l_x, trainable=True)
    updates = nn.updates.adam(loss, params, learning_rate=1e-4)
    train_fn = theano.function([input_var, target_var], loss, updates=updates)
    val_fn = theano.function([input_var, target_var], test_loss)

    print("Starting training...")
    batch_size = 100
    for epoch in range(num_epochs):
        train_err = 0
        train_batches = 0
        start_time = time.time()
        for batch in iterate_minibatches(X_train, X_train_tgt, batch_size, shuffle=True):
            inputs, targets = batch
            this_err = train_fn(inputs, targets)
            train_err += this_err
            train_batches += 1
        val_err = 0
        val_batches = 0
        for batch in iterate_minibatches(X_val, X_val_tgt, batch_size, shuffle=False):
            inputs, targets = batch
            err = val_fn(inputs, targets)
            val_err += err
            val_batches += 1
        print("Epoch {} of {} took {:.3f}s".format(
            epoch + 1, num_epochs, time.time() - start_time))
        print("  training loss:\t\t{:.6f}".format(train_err / train_batches))
        print("  validation loss:\t\t{:.6f}".format(val_err / val_batches))

    test_err = 0
    test_batches = 0
    for batch in iterate_minibatches(X_test, X_test_tgt, batch_size, shuffle=False):
        inputs, targets = batch
        err = val_fn(inputs, targets)
        test_err += err
        test_batches += 1
    test_err /= test_batches
    print("Final results:")
    print("  test loss:\t\t\t{:.6f}".format(test_err))
    # save some example pictures so we can see what it's done 
    example_batch_size = 20
    X_comp = X_test[:example_batch_size]
    pred_fn = theano.function([input_var], test_prediction)
    X_pred = pred_fn(X_comp).reshape(-1, 1, width, height)
    for i in range(20):
        get_image_pair(X_comp, X_pred, idx=i, channels=1).save('output_{}.jpg'.format(i))
    # save the parameters so they can be loaded for next time
    print("Saving")
    fn = 'p_{:.6f}.params'.format(test_err)
    pickle.dump(nn.layers.get_all_param_values(l_x), open(fn, 'w'))

    # sample from latent space if it's 2d
    if L == 2:
        im = Image.new('L', (width*19,height*19))
        for (x,y),val in np.ndenumerate(np.zeros((19,19))):
            z = np.asarray([norm.ppf(0.05*(x+1)), norm.ppf(0.05*(y+1))],
                    dtype=theano.config.floatX)
            x_gen = gen_fn(z).reshape(-1, 1, width, height)
            im.paste(Image.fromarray(get_image_array(x_gen,0)), (x*width,y*height))
            im.save('gen.jpg')

if __name__ == '__main__':
    # Arguments - integers, except for binary/continous. Default uses binary.
    # Run with option --continuous for continuous output.
    import argparse
    parser = argparse.ArgumentParser(description='Command line options')
    parser.add_argument('--num_epochs', type=int, dest='num_epochs')
    parser.add_argument('--L', type=int, dest='L')
    parser.add_argument('--z_dim', type=int, dest='z_dim')
    parser.add_argument('--n_hid', type=int, dest='n_hid')
    parser.add_argument('--binary', dest='binary', action='store_true')
    parser.add_argument('--continuous', dest='binary', action='store_false')
    parser.set_defaults(binary=True)
    args = parser.parse_args(sys.argv[1:])
    main(**{k:v for (k,v) in vars(args).items() if v is not None})
