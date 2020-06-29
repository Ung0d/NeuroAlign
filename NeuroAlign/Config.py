#NeuroAlign parameter configuration
config = {

    "type" : "protein",  #currently supports nucleotide or protein

    #"num_col" : 250,

    #number of sequentially applied core networks (each with unique parameters)
    "num_kernel" : 1,

    #iteration counts for the different components
    "train_iterations" : 100,
    "test_iterations" : 100,

    #training performance and logging
    "learning_rate" : 1e-6,
    "num_training_iteration" : 5000,
    "batch_size": 100,
    "savestate_milestones": 50,
    "l2_regularization" : 1e-10,
    "adjacent_column_radius" : 3000,
    "window_uniform_radius" : 40,

    #hidden dimension for the latent representations for each alphabet symbol
    #for simplicity also used for the representations of their interactions (edges) and their global representation
    "alphabet_network_layers" : [100],

    #hidden dimension for the latent representations for each sequence position
    #for simplicity also used for the representations of the forward edges the along sequences and
    #the global representation for each sequence
    "seq_latent_dim" : 100,

    "seq_global_dim" : 100,

    "seq_net_node_layers" : [100,100],
    "seq_net_edge_layers" : [100,100],
    "seq_net_global_per_seq_layers" : [100,100],
    "seq_global_layers" : [100,100],

    "alphabet_to_sequence_layers" : [100],
    "columns_to_sequence_layers" : [100],

    #hidden dimension for the latent representation of the global property of each column
    "col_latent_dim" : 100,

    "column_net_node_layers" : [100,100],
    "column_net_global_layers" : [100,100],
    "column_decode_node_layers" : [100,100],

    "sequence_to_columns_layers" : [100]
}
