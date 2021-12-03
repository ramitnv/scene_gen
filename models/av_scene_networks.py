'''
Inspired by
https://github.com/lyft/l5kit/blob/master/l5kit/l5kit/planning/vectorized/open_loop_model.py

'''
import math
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as nnf

#########################################################################################


class PointNet(nn.Module):

    def __init__(self, n_layers, d_in, d_out, d_hid):
        super(PointNet, self).__init__()
        self.n_layers = n_layers
        self.d_in = d_in
        self.d_out = d_out
        self.d_hid = d_hid
        self.layer_dims = [d_in] + (n_layers - 1) * [d_hid] + [d_out]
        self.matA = {}
        self.matB = {}
        for i_layer in range(n_layers - 1):
            # each layer the function that operates on each element in the set x is
            # f(x) = ReLu(A x + B * (sum over all non x elements) )
            layer_dims = (self.layer_dims[i_layer + 1], self.layer_dims[i_layer])
            self.matA[i_layer] = nn.Parameter(torch.Tensor(*layer_dims))
            self.matB[i_layer] = nn.Parameter(torch.Tensor(*layer_dims))
            self.register_parameter(name=f'matA_{i_layer}', param=self.matA[i_layer])
            self.register_parameter(name=f'matB_{i_layer}', param=self.matB[i_layer])

            # PyTorch's default initialization:
            nn.init.kaiming_uniform_(self.matA[i_layer], a=math.sqrt(5))
            nn.init.kaiming_uniform_(self.matB[i_layer], a=math.sqrt(5))
        self.out_layer = nn.Linear(d_hid, d_out)
        self.layer_normalizer = nn.LayerNorm(d_hid)

    def forward(self, in_set):
        ''''
             each layer the function that operates on each element in the set x is
            f(x) = ReLu(A x + B * (sum over all non x elements) )
            where A and B are the same for all elements, and are layer dependent.
            After that the elements are aggregated by max-pool
             and finally  a linear layer gives the output

            input is a tensor of size [num_set_elements x elem_dim]

        '''
        h = in_set
        n_elements = in_set.shape[0]
        for i_layer in range(self.n_layers - 1):
            matA = self.matA[i_layer]
            matB = self.matB[i_layer]
            pre_layer_sum = h.sum(dim=0).squeeze()
            h_new_lst = []
            for i_elem in range(n_elements):
                h_elem = h[i_elem].squeeze()
                sum_without_elem = pre_layer_sum - h_elem
                h_new = matA @ h_elem + matB @ sum_without_elem
                h_new_lst.append(h_new)
            h = torch.stack(h_new_lst)
            h = self.layer_normalizer(h)
            h = nnf.relu(h)
        # apply permutation invariant aggregation over all elements
        # max-pooling in our case
        h = torch.max(h, dim=0).values
        h = self.out_layer(h)
        return h
#########################################################################################


class PolygonEncoder(nn.Module):

    def __init__(self, dim_latent, n_conv_layers, kernel_size, max_points_per_poly):
        super(PolygonEncoder, self).__init__()
        self.dim_latent = dim_latent
        self.n_conv_layers = n_conv_layers
        self.kernel_size = kernel_size
        self.max_points_per_poly = max_points_per_poly
        self.conv_layers = []
        for i_layer in range(self.n_conv_layers):
            if i_layer == 0:
                in_channels = 2  # in the input each point has 2 channels (x,y)
            else:
                in_channels = self.dim_latent
            self.conv_layers.append(nn.Conv1d(in_channels=in_channels,
                                              out_channels=self.dim_latent,
                                              kernel_size=self.kernel_size,
                                              padding='same',
                                              padding_mode='circular'))
        self.layers = nn.ModuleList(self.conv_layers)
        self.layer_normalizer = nn.LayerNorm([1, self.dim_latent, max_points_per_poly])
        self.out_layer = nn.Linear(self.dim_latent, self.dim_latent)

    def forward(self, poly_points):
        """Standard forward
        input [1 x n_points  x 2 coordinates]
        """
        assert poly_points.shape[0] == 1  # assume batch_size=1
        n_points_orig = poly_points.shape[1]
        assert n_points_orig <= self.max_points_per_poly

        if n_points_orig < self.max_points_per_poly:
            # Pad to fixed size, using wrap padding (that keeps the circular invariance of the embedding)
            # h = nnf.pad(poly_points, (0, self.max_points_per_poly - n_points_orig), mode='circular') # not implemented yet in PyTorch for 1d
            h = np.pad(poly_points,
                       ((0, 0), (0, self.max_points_per_poly - n_points_orig), (0, 0)),
                       mode='wrap')
            h = torch.from_numpy(h)
        else:
            h = poly_points

        # fit to conv1d input dimensions [batch_size=1  x in_channels=2  x n_points]
        h = torch.permute(h, (0, 2, 1))
        # We use several layers a 1d circular convolution followed by ReLu (equivariant layers)
        # and finally sum the output - this is all in all - a shift-invariant operator
        for i_layer in range(self.n_conv_layers):
            h = self.conv_layers[i_layer](h)
            h = self.layer_normalizer(h)
            h = nnf.relu(h)
        h = h.sum(dim=2)
        h = self.out_layer(h)
        return h
#########################################################################################


class MapEncoder(nn.Module):

    def __init__(self, opt):
        super(MapEncoder, self).__init__()
        self.polygon_name_order = opt.polygon_name_order
        self.dim_latent_polygon_elem = opt.dim_latent_polygon_elem
        n_polygon_types = len(opt.polygon_name_order)
        self.n_point_net_layers = 3
        self.dim_latent_polygon_type = opt.dim_latent_polygon_type
        self.dim_latent_map = opt.dim_latent_map
        self.poly_encoder = nn.ModuleList()
        self.sets_aggregators = nn.ModuleList()
        for _ in self.polygon_name_order:
            self.poly_encoder.append(
                PolygonEncoder(dim_latent=self.dim_latent_polygon_elem,
                               n_conv_layers=opt.n_conv_layers_polygon,
                               kernel_size=opt.kernel_size_conv_polygon,
                               max_points_per_poly=opt.max_points_per_poly))
            self.sets_aggregators.append(PointNet(n_layers=self.n_point_net_layers,
                                                  d_in=self.dim_latent_polygon_elem,
                                                  d_out=self.dim_latent_polygon_type,
                                                  d_hid=self.dim_latent_polygon_type))
        self.poly_types_aggregator = \
            torch.nn.Linear(in_features=self.dim_latent_polygon_type * n_polygon_types,
                            out_features=self.dim_latent_map)

    def forward(self, input):
        """Standard forward
        """
        poly_types_latents = []
        for i_poly_type, poly_type_name in enumerate(self.polygon_name_order):
            # Get the latent embedding of all elements of this type of polygons:
            poly_encoder = self.poly_encoder[i_poly_type]
            poly_elements = input[poly_type_name]
            poly_latent_per_elem = []
            for poly_elem in poly_elements:
                # Transform from sequence of points to a fixed size vector,
                # using a circular-shift-invariant module
                poly_latent = poly_encoder(poly_elem)
                poly_latent_per_elem.append(poly_latent)
            # Run PointNet to aggregate all polygon elements of this  polygon type
            poly_latent_per_elem = torch.stack(poly_latent_per_elem)
            elem_agg = self.sets_aggregators[i_poly_type]
            poly_types_latents.append(elem_agg(poly_latent_per_elem))
        poly_types_latents = torch.cat(poly_types_latents)
        map_latent = self.poly_types_aggregator(poly_types_latents)
        return map_latent
#########################################################################################



class AgentsDecoder(nn.Module):

    def __init__(self, opt):
        super(AgentsDecoder, self).__init__()

    def forward(self, scene_latent):
        agents_feat = None
        return agents_feat
#########################################################################################


class SceneGenerator(nn.Module):

    def __init__(self, opt):
        super(SceneGenerator, self).__init__()
        self.map_enc = MapEncoder(opt)
        # Debug - print parameter names:
        # [x[0] for x in self.named_parameters()]
        self.dim_latent_scene_noise = opt.dim_latent_scene_noise
        self.batch_size = opt.batch_size
        if self.batch_size != 1:
            raise NotImplementedError

    def forward(self, map_feat):
        """Standard forward"""
        map_latent = self.map_enc(map_feat)
        latent_noise = torch.randn(self.dim_latent_scene_noise)
        scene_latent = torch.concat([map_latent, latent_noise], dim=0)
        return scene_latent
#########################################################################################333