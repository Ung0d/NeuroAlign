import tensorflow as tf
import graph_nets as gn
import sonnet as snt
import numpy as np
import os


#all involved neural networks are MLPs with layer sizes defined in Config
#each layer is relu activated
#layer normalization is applied after the last layer
def make_mlp_model(layersizes):
    return lambda: snt.Sequential([
        snt.nets.MLP(layersizes, activate_final=True),
        snt.LayerNorm(axis=-1, create_offset=True, create_scale=True)
        ])

#reduces all graphs in the given graph tuple to a single graph by applying reducer to nodes, edges and globals
def reduce_graphs(graphs):
    n_graphs = gn.utils_tf.get_num_graphs(graphs)
    n_segs = graphs.n_node[0]
    segments = tf.tile(tf.range(n_segs), [n_graphs])
    nodes = tf.math.unsorted_segment_mean(graphs.nodes, segments, n_segs)
    globals = tf.reduce_mean(graphs.globals, axis = 0, keepdims=True)
    return graphs.replace(nodes = nodes, globals = globals)

#converts a 1D int tensor of lenghts to a vector of ids required for segment_mean calls
def make_seq_ids(lens):
    c = tf.cumsum(lens)
    idx = c[:-1]
    s = tf.scatter_nd(tf.expand_dims(idx, 1), tf.ones_like(idx), [c[-1]])
    return tf.cumsum(s)


class DeepSet(gn._base.AbstractModule):

  def __init__(self,
               node_model_fn,
               global_model_fn,
               reducer=tf.math.unsorted_segment_sum,
               name="deep_set"):
    super(DeepSet, self).__init__(name=name)

    with self._enter_variable_scope():
        self._node_block = gn.blocks.NodeBlock(
            node_model_fn=node_model_fn,
            use_received_edges=False,
            use_sent_edges=False,
            use_nodes=True,
            use_globals=True)
        self._global_block = gn.blocks.GlobalBlock(
            global_model_fn=global_model_fn,
            use_edges=False,
            use_nodes=True,
            use_globals=True,
            nodes_reducer=reducer)

  def _build(self, graph):
    return self._node_block(self._global_block(graph))


def tile_like(graph, template_graphs):
    multiples = [gn.utils_tf.get_num_graphs(template_graphs), 1]
    return template_graphs.replace(nodes = tf.tile(graph.nodes, multiples),
                                      globals = tf.tile(graph.globals, multiples))


#encodes the inputs of the neuroalign model
#after encoding, one or more NeuroAlign Cores follow that them the
#burden of optimization
class NeuroAlignEncoder(snt.Module):

    def __init__(self, config, name = "NeuroAlignEncoder"):

        super(NeuroAlignEncoder, self).__init__(name=name)

        self.seq_encoder = gn.modules.GraphIndependent(
                            edge_model_fn=make_mlp_model(config["seq_enc_edge_layer_s"]),
                            node_model_fn=make_mlp_model(config["seq_enc_node_layer_s"]),
                            global_model_fn=make_mlp_model(config["seq_enc_globals_layer_s"]))

        self.mem_encoder = gn.modules.GraphIndependent(
                            node_model_fn=make_mlp_model(config["mem_enc_node_layer_s"]),
                            global_model_fn=make_mlp_model(config["mem_enc_globals_layer_s"]))

    def __call__(self,
                sequence_graph, #input graph with position nodes and forward edges describing the sequences
                col_priors): #a graph tuple with a col prior graph for each column

        latent_seq_graph = self.seq_encoder(sequence_graph)
        latent_mem = self.mem_encoder(col_priors)
        return latent_seq_graph, latent_mem


#core module that computes the NeuroAlign prediction
#output has to be passed to the NeuroAlignDecoder
class NeuroAlignCore(snt.Module):

    def __init__(self, config, name = "NeuroAlignCore"):

        super(NeuroAlignCore, self).__init__(name=name)

        self.column_network = DeepSet(
                                    node_model_fn=make_mlp_model(config["column_net_node_layers"]),
                                    global_model_fn=make_mlp_model(config["column_net_global_layers"]),
                                    reducer = tf.math.unsorted_segment_mean)

        self.seq_network = gn.modules.GraphNetwork(
                                    edge_model_fn=make_mlp_model(config["seq_net_edge_layers"]),
                                    node_model_fn=make_mlp_model(config["seq_net_node_layers"]),
                                    global_model_fn=make_mlp_model(config["seq_net_global_layers"]),
                                    reducer = tf.math.unsorted_segment_mean)

    def __call__(self, latent_seq_graph, latent_mem, num_iterations):

        latent_mem0 = latent_mem
        tiled_latent_seq_graph0 = tile_like(latent_seq_graph, latent_mem)
        for _ in range(num_iterations):
            tiled_latent_seq = tile_like(latent_seq_graph, latent_mem)
            col_input = gn.utils_tf.concat([tiled_latent_seq, latent_mem, latent_mem0], axis=1)
            latent_mem = self.column_network(col_input)
            seq_input = gn.utils_tf.concat([tiled_latent_seq_graph0, tiled_latent_seq, latent_mem], axis=1)
            reduced = reduce_graphs(self.seq_network(seq_input))
            latent_seq_graph = latent_seq_graph.replace(nodes = reduced.nodes, globals = reduced.globals)
        return latent_seq_graph, latent_mem



#decodes the output of a NeuroAlign core module
class NeuroAlignDecoder(snt.Module):

    def __init__(self, config, name = "NeuroAlignDecoder"):

        super(NeuroAlignDecoder, self).__init__(name=name)

        self.seq_decoder = gn.modules.GraphIndependent(node_model_fn=make_mlp_model(config["seq_dec_node_layer_s"]))

        self.seq_output_transform = gn.modules.GraphIndependent(node_model_fn = lambda: snt.Linear(1, name="seq_output"))

        self.mem_decoder = gn.modules.GraphIndependent(node_model_fn=make_mlp_model(config["mem_dec_node_layer_s"]),
                                global_model_fn=make_mlp_model(config["mem_dec_global_layer_s"]))

        self.mem_output_transform = gn.modules.GraphIndependent(node_model_fn = lambda: snt.Linear(1, name="mem_node_output"),
                                        global_model_fn = lambda: snt.Linear(1, name="mem_global_output"))


    def __call__(self, latent_seq_graph, seq_lens, latent_mem):

        #decode and convert to 1d outputs
        seq_out = self.seq_output_transform(self.seq_decoder(latent_seq_graph))
        mem_out = self.mem_output_transform(self.mem_decoder(latent_mem))

        #get predicted relative positions of sequence nodes and columns
        node_relative_positions = seq_out.nodes
        col_relative_positions = mem_out.globals

        #predict the distribution of the seq nodes to the columns using
        #a product of 2 softmaxes
        mem_per_col = tf.reshape(mem_out.nodes, [latent_seq_graph.n_node[0], -1]) # (num_node, num_pattern)-tensor
        exp_mem_per_col = tf.exp(mem_per_col)
        sum_along_cols = tf.reduce_sum(exp_mem_per_col, axis=1, keepdims=True)+1 #+1 to account for softmax uncertainty
        s_nodes = exp_mem_per_col/sum_along_cols

        sum_along_seqs = tf.math.segment_sum(exp_mem_per_col, make_seq_ids(seq_lens))+1 #-> (num_segments, num_pattern)-tensor
        s_cols = exp_mem_per_col/tf.repeat(sum_along_seqs, repeats = seq_lens, axis = 0)

        return node_relative_positions, col_relative_positions, s_nodes, s_cols


#a module that computes the NeuroAlign prediction
#output is a graph with 1D node and 1D edge attributes
#nodes are sequence and pattern nodes with respective relative positions
#edges correspond to region to pattern memberships with logits from which the
#degrees of membership can be computed
class NeuroAlignModel(snt.Module):

    def __init__(self, config, name = "NeuroAlignModel"):

        super(NeuroAlignModel, self).__init__(name=name)
        self.enc = NeuroAlignEncoder(config)
        self.cores = [NeuroAlignCore(config) for _ in range(config["num_nr_core"])]
        self.dec = NeuroAlignDecoder(config)


    def __call__(self,
                sequence_graph, #input graph with position nodes and forward edges describing the sequences
                seq_lens, #specifies the length of each input sequence, i.e. seq_lens[i] is the length of the ith chain in sequence_graph
                col_priors, #a graph tuple with a col prior graph for each column
                num_iterations): #the number of message passing iterations

        latent_seq_graph, latent_mem = self.enc(sequence_graph, col_priors)
        for core in self.cores:
            latent_seq_graph, latent_mem = core(latent_seq_graph, latent_mem, num_iterations)
        return self.dec(latent_seq_graph, seq_lens, latent_mem)





class NeuroAlignPredictor():

    def __init__(self, config, examle_msa):

        self.model = NeuroAlignModel(config)
        self.checkpoint = tf.train.Checkpoint(module=self.model)
        self.checkpoint_root = "./checkpoints"
        self.checkpoint_name = "NeuroAlign"
        self.save_prefix = os.path.join(self.checkpoint_root, self.checkpoint_name)

        def inference(sequence_graph, seq_lens, col_priors):
            return self.model(sequence_graph, seq_lens, col_priors, config["test_mp_iterations"])

        seq_input_example, col_input_example = self._graphs_from_instance(examle_msa, 1)

        # Get the input signature for that function by obtaining the specs
        self.input_signature = [
          gn.utils_tf.specs_from_graphs_tuple(seq_input_example),
          tf.TensorSpec((None), dtype=tf.dtypes.int32),
          gn.utils_tf.specs_from_graphs_tuple(col_input_example, dynamic_num_graphs=True)
        ]

        # Compile the update function using the input signature for speedy code.
        self.inference = tf.function(inference, input_signature=self.input_signature)

    #col_priors is a list of position pairs (s,i) = sequence s at index i
    def predict(self, msa, num_cols):
        seq_g, col_g = self._graphs_from_instance(msa, num_cols)
        node_relative_pos, col_relative_pos, s_nodes, s_cols = self.inference(seq_g, tf.constant(msa.seq_lens), col_g)
        return node_relative_pos.numpy(), col_relative_pos.numpy(), s_nodes.numpy(), s_cols.numpy()

    def _graphs_from_instance(self, msa, num_cols):
        seq_dict = {"globals" : [np.float32(0)],
                        "nodes" : msa.nodes,
                        "edges" : np.zeros((len(msa.forward_senders), 1), dtype=np.float32),
                        "senders" : msa.forward_senders,
                        "receivers" : msa.forward_receivers }
        col_dicts = []
        for i in range(num_cols):
            # col_nodes = np.zeros((msa.nodes.shape[0],1), dtype=np.float32)
            # col_nodes[sum(msa.seq_lens[:s]) + i] = 1
            col_dicts.append({"globals" : [np.float32(i/num_cols)],
            "nodes" : msa.nodes,
            "senders" : [],
            "receivers" : [] })
        seq_g = gn.utils_tf.data_dicts_to_graphs_tuple([seq_dict])
        col_g = gn.utils_tf.data_dicts_to_graphs_tuple(col_dicts)
        col_g = gn.utils_tf.set_zero_edge_features(col_g, 0)
        return seq_g, col_g


    def load_latest(self):
        latest = tf.train.latest_checkpoint(self.checkpoint_root)
        if latest is not None:
            self.checkpoint.restore(latest)
            print("Loaded latest checkpoint")


    def save(self):
        self.checkpoint.save(self.save_prefix)
        print("Saved current model.")
