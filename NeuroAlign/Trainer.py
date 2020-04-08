import tensorflow as tf
import graph_nets as gn
import sonnet as snt
import numpy as np

import Model


#optimizes the NeuroAlign model using a dataset of reference alignments
class NeuroAlignTrainer():

    def __init__(self, config, predictor):
        self.config = config
        self.predictor = predictor
        optimizer = snt.optimizers.Adam(config["learning_rate"])
        def train_step(sequence_graph, col_priors, len_seqs,
                        target_node_rp, target_mems, rel_occ_per_col):
            with tf.GradientTape() as tape:
                out = self.predictor.model(sequence_graph, len_seqs, col_priors, config["train_mp_iterations"])
                train_loss = 0
                for n_rp, c_rp, rel_occ, mem in out[-1:]:
                    l_node_rp = tf.compat.v1.losses.mean_squared_error(target_node_rp, n_rp)
                    l_col_rp = tf.compat.v1.losses.mean_squared_error(col_priors.globals, c_rp)
                    l_rel_occ = tf.reduce_mean(tf.nn.softmax_cross_entropy_with_logits(labels = rel_occ_per_col, logits = rel_occ))
                    #l_mem_logs = tf.compat.v1.losses.sigmoid_cross_entropy(tf.one_hot(target_col_segment_ids, gn.utils_tf.get_num_graphs(col_priors)), mem_logits)
                    l_mem_logs = tf.compat.v1.losses.log_loss(target_mems, mem)   #tf.one_hot(target_col_segment_ids, gn.utils_tf.get_num_graphs(col_priors))
                    l_node_rp = l_node_rp*config["lambda_node_rp"]
                    l_col_rp = l_col_rp*config["lambda_col_rp"]
                    l_rel_occ = l_rel_occ*config["lambda_rel_occ"]
                    l_mem_logs = l_mem_logs*config["lambda_mem"]
                    train_loss += l_node_rp + l_col_rp + l_rel_occ + l_mem_logs
                #train_loss /= config["num_nr_core"]*config["train_mp_iterations"]
                regularizer = snt.regularizers.L2(config["l2_regularization"])
                train_loss += regularizer(self.predictor.model.trainable_variables)
                gradients = tape.gradient(train_loss, self.predictor.model.trainable_variables)
                optimizer.apply(gradients, self.predictor.model.trainable_variables)
                return n_rp, c_rp, rel_occ, mem, train_loss, l_node_rp, l_col_rp, l_rel_occ, l_mem_logs

        # Get the input signature for that function by obtaining the specs
        len_alphabet = 4 if config["type"] == "nucleotide" else 23
        self.input_signature = [
            tf.TensorSpec((None, 1), dtype=tf.dtypes.float32),
            tf.TensorSpec((None,None), dtype=tf.dtypes.int32),
            tf.TensorSpec((None, len_alphabet+1), dtype=tf.dtypes.float32)
        ]

        # Compile the update function using the input signature for speedy code.
        self.step_op = tf.function(train_step, input_signature=predictor.input_signature + self.input_signature)

    #supervised training using reference alignment
    #randomly select a center columns with a window of adjacent columns within
    #a certain radius (if existing)
    def train(self, msa):
        c = np.random.randint(msa.alignment_len)
        seq_g, col_g = self.predictor._graphs_from_instance_window(msa, msa.alignment_len, c)
        r = self.config["adjacent_column_radius"]
        lb = max(0,c-r)
        ub = min(msa.alignment_len, c+r+1)
        rocc = msa.rel_occ_per_column[lb:ub,:]
        mem_one_hot = np.zeros((sum(msa.seq_lens), ub-lb))
        for i,tar in enumerate(msa.membership_targets):
            if tar >= lb and tar < ub:
                mem_one_hot[i, tar-lb] = 1
        return self.step_op(seq_g, col_g, tf.constant(msa.seq_lens), msa.node_rp_targets, mem_one_hot, rocc)
