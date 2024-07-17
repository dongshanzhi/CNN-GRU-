#-*- coding:utf-8 -*-
import numpy as np
import theano
import theano.tensor as T

import sys, getopt
import logging

from state import *
from utils import *
from SS_dataset import *

import itertools
import sys
import pickle
import random
import datetime
import math
import copy

logger = logging.getLogger(__name__)


def add_random_variables_to_batch(state, rng, batch, prev_batch = None):
    """
    This is a helper function, which adds the Normal random variables in a batch.
    We do it this way, because we want to avoid Theano's random sampling both to speed up and to avoid
    known Theano issues with sampling inside scan loops.

    Currently only the random variable 'ran_var_constutterance. is sampled from a standard Normal distribution, 
    which remains constant during each utterance (i.e. between end-of-utterance tokens).
    """

    # If none return none...
    if not batch:
        return batch

    # Variable to store random vector sampled at the beginning of each utterance
    Ran_Var_ConstUtterance = numpy.zeros((batch['x'].shape[0], batch['x'].shape[1], state['latent_gaussian_per_utterance_dim']), dtype='float32')
    
    #print 'latent_gaussian_per_utterance_dim',state['latent_gaussian_per_utterance_dim']
        
    # Go through each sample, find end-of-utterance indices and sample random variables
    for idx in xrange(batch['x'].shape[1]):
        # Find end-of-utterance indices
        eos_indices = numpy.where(batch['x'][:, idx] == state['eos_sym'])[0].tolist()

        # Make sure we also sample at the beginning of the utterance, and that we stop appropriately at the end
        if len(eos_indices) > 0:
            if not eos_indices[0] == 0:
                eos_indices = [0] + eos_indices
            if not eos_indices[-1] == batch['x'].shape[0]:
                eos_indices = eos_indices + [batch['x'].shape[0]]
        else:
            eos_indices = [0] + [batch['x'].shape[0]-1]

        # Sample random variables using NumPy
        ran_vectors = rng.normal(loc=0, scale=1, size=(len(eos_indices), state['latent_gaussian_per_utterance_dim']))
        for i in range(len(eos_indices)-1):
            for j in range(eos_indices[i], eos_indices[i+1]):
                Ran_Var_ConstUtterance[j, idx, :] = ran_vectors[i, :]

        # If a previous batch is given, and the last utterance in the previous batch
        # overlaps with the first utterance in the current batch, then we need to copy over 
        # the random variables from the last utterance in the last batch to remain consistent.
        if prev_batch:
            if ('x_reset' in prev_batch) and (not numpy.sum(numpy.abs(prev_batch['x_reset'])) < 1) \
              and ('ran_var_constutterance' in prev_batch):
                prev_ran_vector = prev_batch['ran_var_constutterance'][-1,idx,:]
                if len(eos_indices) > 1:
                    for j in range(0, eos_indices[1]):
                        Ran_Var_ConstUtterance[j, idx, :] = prev_ran_vector
                else:
                    for j in range(0, batch['x'].shape[0]):
                        Ran_Var_ConstUtterance[j, idx, :] = prev_ran_vector


    # Add new random variables to batch and return the new batch
    batch['ran_var_constutterance'] = Ran_Var_ConstUtterance

    return batch


def create_padded_batch(state, rng, x, force_end_of_utterance_token = False):
    # Find max length in batch
    mx = 0
    for idx in xrange(len(x[0])):
        mx = max(mx, len(x[0][idx]))

    # Take into account that sometimes we need to add the end-of-utterance symbol at the start
    mx += 1

    n = state['bs'] 
    
    X = numpy.zeros((mx, n), dtype='int32')
    Xmask = numpy.zeros((mx, n), dtype='float32') 

    # Variable to store each utterance in reverse form (for bidirectional RNNs)
    X_reversed = numpy.zeros((mx, n), dtype='int32')

    # Variable to store random vector sampled at the beginning of each utterance
    #Ran_Var_ConstUtterance = numpy.zeros((mx, n, state['latent_gaussian_per_utterance_dim']), dtype='float32')

    # Fill X and Xmask
    # Keep track of number of predictions and maximum dialogue length
    num_preds = 0
    max_length = 0
    #print 'init x:',x
    #print 'len for :',len(x[0])
    for idx in xrange(len(x[0])):
        # Insert sequence idx in a column of matrix X
        dialogue_length = len(x[0][idx])

        # Fiddle-it if it is too long ..
        if mx < dialogue_length: 
            continue

        # Make sure end-of-utterance symbol is at beginning of dialogue.
        # This will force model to generate first utterance too
        if not x[0][idx][0] == state['eos_sym']:
            X[:dialogue_length+1, idx] = [state['eos_sym']] + x[0][idx][:dialogue_length]
            dialogue_length = dialogue_length + 1
        else:
            X[:dialogue_length, idx] = x[0][idx][:dialogue_length]

        max_length = max(max_length, dialogue_length)

        # Set the number of predictions == sum(Xmask), for cost purposes, minus one (to exclude first eos symbol)
        num_preds += dialogue_length - 1
        
        # Mark the end of phrase
        if len(x[0][idx]) < mx:
            if force_end_of_utterance_token:
                X[dialogue_length:, idx] = state['eos_sym']

        # Initialize Xmask column with ones in all positions that
        # were just set in X (except for first eos symbol, because we are not evaluating this). 
        # Note: if we need mask to depend on tokens inside X, then we need to 
        # create a corresponding mask for X_reversed and send it further in the model
        Xmask[0:dialogue_length, idx] = 1.

        # Reverse all utterances
        eos_indices = numpy.where(X[:, idx] == state['eos_sym'])[0]
        X_reversed[:, idx] = X[:, idx]
        prev_eos_index = -1
        for eos_index in eos_indices:
            X_reversed[(prev_eos_index+1):eos_index, idx] = (X_reversed[(prev_eos_index+1):eos_index, idx])[::-1]
            prev_eos_index = eos_index
            if prev_eos_index > dialogue_length:
                break

        # Sample random variables (we want to avoid Theano's random sampling to speed up the process...)
        #ran_vectors = rng.normal(loc=0, scale=1, size=(len(eos_indices), state['latent_gaussian_per_utterance_dim']))
        #for i in range(len(eos_indices)-1):
        #    for j in range(eos_indices[i], eos_indices[i+1]):
        #        Ran_Var_ConstUtterance[j, idx, :] = ran_vectors[i, :]

        #print 'X[:dialogue_length, idx]', X[:dialogue_length, idx]
        #print 'Ran_Var_ConstUtterance[j, :, idx]', Ran_Var_ConstUtterance[:, :, idx]
        
        #print 'xxxx: ',X

    assert num_preds == numpy.sum(Xmask) - numpy.sum(Xmask[0, :])

    return {'x': X,                                                 \
            'x_reversed': X_reversed,                               \
            'x_mask': Xmask,                                        \
            'num_preds': num_preds,                                 \
            'num_dialogues': len(x[0]),                             \
            'max_length': max_length                                \
           }
            #'ran_var_constutterance': Ran_Var_ConstUtterance,       \

class Iterator(SSIterator):
    def __init__(self, dialogue_file, batch_size, **kwargs):
        SSIterator.__init__(self, dialogue_file, batch_size,                   \
                            semantic_file=kwargs.pop('semantic_file', None), \
                            max_len=kwargs.pop('max_len', -1),               \
                            use_infinite_loop=kwargs.pop('use_infinite_loop', False))
        # TODO: max_len should be handled here and SSIterator should zip semantic_data and 
        # data. 
        self.k_batches = kwargs.pop('sort_k_batches', 20) #k_batches默认值为20
        # TODO: For backward compatibility. This should be removed in future versions
        # i.e. remove all the x_reversed computations in the model itself.
        self.state = kwargs.pop('state', None)
        # ---------------- 
        self.batch_iter = None
        self.rng = numpy.random.RandomState(self.state['seed'])

        # Keep track of previous batch, because this is needed to specify random variables
        self.prev_batch = None

    def get_homogenous_batch_iter(self, batch_size = -1):
        while True:
            batch_size = self.batch_size if (batch_size == -1) else batch_size 
           
            data = []
            for k in range(self.k_batches):
                batch = SSIterator.next(self)
                if batch:
                    data.append(batch)
            
            if not len(data):
                return
            
            number_of_batches = len(data)
            data = list(itertools.chain.from_iterable(data))
            
            #测试代码
            """
            if Test_Print_Flag:
                pass
                print 'data:\t',data[0]
                #print '*'*30
                #for it in data:
                #    print it
                #print '*'*30
            """

            # Split list of words from the dialogue index
            data_x = []
            data_semantic = []
            for i in range(len(data)): #data长度为20，第一段对话和第二段对话交叉出现（但并不交替）
                data_x.append(data[i][0]) #data_x字段存储一段对话
                data_semantic.append(data[i][1]) #我理解是存储对话的指纹，表明这是哪一段对话

            x = numpy.asarray(list(itertools.chain(data_x)))
            #print 'init before for x:', len(x)
            #print 'number_of_batches',number_of_batches
            x_semantic = numpy.asarray(list(itertools.chain(data_semantic)))

            lens = numpy.asarray([map(len, x)])
            order = numpy.argsort(lens.max(axis=0))
                 
            for k in range(number_of_batches): #循环20次
                indices = order[k * batch_size:(k + 1) * batch_size]
                full_batch = create_padded_batch(self.state, self.rng, [x[indices]])

                # Add semantic information to batch; take care to fill with -1 (=n/a) whenever the batch is filled with empty dialogues
#                if 'semantic_information_dim' in self.state:
                if self.semantic_file:
                    full_batch['x_semantic'] = - numpy.ones((self.state['bs'], self.state['semantic_information_dim'])).astype('int32')
                    full_batch['x_semantic'][0:len(indices), :] = numpy.asarray(list(itertools.chain(x_semantic[indices]))).astype('int32')
                else:
                    full_batch['x_semantic'] = None

                # Then split batches to have size 'max_grad_steps'
                splits = int(math.ceil(float(full_batch['max_length']) / float(self.state['max_grad_steps'])))
                #print 'splits:\t',splits
                batches = []
                for i in range(0, splits):#这里会按照state['max_grad_steps']值将一段对话分割成多个部分，每个部分包含的字符数不得大于该值
                    batch = copy.deepcopy(full_batch)

                    # Retrieve start and end position (index) of current mini-batch
                    start_pos = self.state['max_grad_steps'] * i
                    if start_pos > 0:
                        start_pos = start_pos - 1

                    # We need to copy over the last token from each batch onto the next, 
                    # because this is what the model expects.
                    end_pos = min(full_batch['max_length'], self.state['max_grad_steps'] * (i + 1))

                    batch['x'] = full_batch['x'][start_pos:end_pos, :]
                    batch['x_reversed'] = full_batch['x_reversed'][start_pos:end_pos, :]
                    batch['x_mask'] = full_batch['x_mask'][start_pos:end_pos, :]
                    batch['max_length'] = end_pos - start_pos
                    batch['num_preds'] = numpy.sum(batch['x_mask']) - numpy.sum(batch['x_mask'][0,:])

                    # For each batch we compute the number of dialogues as a fraction of the full batch,
                    # that way, when we add them together, we get the total number of dialogues.
                    batch['num_dialogues'] = float(full_batch['num_dialogues']) / float(splits)
                    batch['x_reset'] = numpy.ones(self.state['bs'], dtype='float32')

                    batches.append(batch)

                if len(batches) > 0:
                    batches[len(batches)-1]['x_reset'] = numpy.zeros(self.state['bs'], dtype='float32')

                #__NNN = 0
                for batch in batches:
                    if batch:
                        #print 'NNN:\t',__NNN
                        #__NNN += 1
                        yield batch


    def start(self):
        SSIterator.start(self)
        self.batch_iter = None

    def next(self, batch_size = -1):
        """ 
        We can specify a batch size,
        independent of the object initialization. 
        """
        # If there are no more batches in list, try to generate new batches
        if not self.batch_iter:
            self.batch_iter = self.get_homogenous_batch_iter(batch_size)

        try:
            # Retrieve next batch
            batch = next(self.batch_iter)

            # Add Normal random variables to batch. 
            # We add them separetly for each batch to save memory.
            # If we instead had added them to the full batch before splitting into mini-batches,
            # the random variables would take up several GBs for big batches and long documents.
            batch = add_random_variables_to_batch(self.state, self.rng, batch, self.prev_batch)
            # Keep track of last batch
            self.prev_batch = batch
        except StopIteration:
            return None
        return batch

def get_train_iterator(state):
    semantic_train_path = None
    semantic_valid_path = None
    
    if 'train_semantic' in state:
        assert state['valid_semantic']
        semantic_train_path = state['train_semantic']
        semantic_valid_path = state['valid_semantic']
    
    train_data = Iterator(
        state['train_dialogues'],
        int(state['bs']),
        state=state,
        seed=state['seed'],
        semantic_file=semantic_train_path,
        use_infinite_loop=True,
        max_len=-1) 
     
    valid_data = Iterator(
        state['valid_dialogues'],
        int(state['bs']),
        state=state,
        seed=state['seed'],
        semantic_file=semantic_valid_path,
        use_infinite_loop=False,
        max_len=-1)
    return train_data, valid_data 

def get_secondary_train_iterator(state):
    secondary_train_data = Iterator(
        state['secondary_train_dialogues'],
        int(state['bs']),
        state=state,
        seed=state['seed'],
        semantic_file=None,
        use_infinite_loop=True,
        max_len=-1) 

    return secondary_train_data

def get_test_iterator(state):
    assert 'test_dialogues' in state
    test_path = state.get('test_dialogues')
    semantic_test_path = state.get('test_semantic', None)

    test_data = Iterator(
        test_path,
        int(state['bs']), 
        state=state,
        seed=state['seed'],
        semantic_file=semantic_test_path,
        use_infinite_loop=False,
        max_len=-1)
    return test_data
