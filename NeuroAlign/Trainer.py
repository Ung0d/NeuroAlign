import tensorflow as tf
import graph_nets as gn
import sonnet as snt


#optimizes the NeuroAlign model using a dataset of reference alignments
class NeuroAlignTrainer():

    def __init__(self, config, predictor):
        self.predictor = predictor
        optimizer = snt.optimizers.Adam(config["learning_rate"])
        def train_step(sequence_graph, seq_lens, col_priors,
                        target_node_rp, target_col_segment_ids):
            with tf.GradientTape() as tape:
                n_rp, c_rp, mem = self.predictor.model(sequence_graph, seq_lens, col_priors, config["train_mp_iterations"])
                l_node_rp = tf.compat.v1.losses.mean_squared_error(target_node_rp, n_rp)
                l_col_rp = tf.compat.v1.losses.mean_squared_error(col_priors.globals, c_rp)
                l_mem = tf.math.unsorted_segment_prod(mem, target_col_segment_ids, gn.utils_tf.get_num_graphs(col_priors))
                l_mem = tf.reduce_mean(l_mem)
                regularizer = snt.regularizers.L2(config["l2_regularization"])
                train_loss = l_node_rp + l_col_rp - l_mem + regularizer(self.predictor.model.trainable_variables)

                gradients = tape.gradient(train_loss, self.predictor.model.trainable_variables)
                optimizer.apply(gradients, self.predictor.model.trainable_variables)
                return n_rp, c_rp, mem, train_loss, l_node_rp, l_col_rp, l_mem

        # Get the input signature for that function by obtaining the specs
        self.input_signature = [
            tf.TensorSpec((None, 1), dtype=tf.dtypes.float32),
            tf.TensorSpec((None), dtype=tf.dtypes.int32)
        ]

        # Compile the update function using the input signature for speedy code.
        self.step_op = tf.function(train_step, input_signature=predictor.input_signature + self.input_signature)

    #use a given reference alignment for training
    def train(self, msa):
        seq_g, col_g = self.predictor._graphs_from_instance(msa, msa.alignment_len)
        return self.step_op(seq_g, msa.seq_lens, col_g, msa.node_rp_targets, msa.membership_targets)