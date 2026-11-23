"""Stage 2 of the LV generation: the street network of a postcode in clusters (``kcid``).

Every connected component of the street graph that has buildings becomes one ``kcid``; a component with more
than ``MAX_BUILDINGS_PER_KCID`` buildings is split with k-means first, because the routed distance matrix of a
``kcid`` grows with the square of its buildings.
"""

import math
import warnings

import numpy as np
from sklearn.cluster import KMeans

from pylovo.config_loader import (
    K_MEANS_SEED,
    MAX_BUILDINGS_PER_KCID,
)

from pylovo.lv_grid.context import Stage


class StreetClustering(Stage):
    """Give every loaded building of the postcode a ``kcid`` (:meth:`run`)."""

    def run(self):
        """
        Find connected components (subgraphs) of an undirected street graph using Depth-First Search algorithm over
        edges and vertices from ways_tem and, if necessary due to their size, apply k-means clustering to these
        street network components.

        FROM: ways_tem, buildings_tem
        INTO: ways_tem, vertices_pgr, buildings_tem
        """

        # Get connected components from the street network
        component, vertices = self.dbc.get_connected_component()
        component_ids = np.unique(component)

        if len(component_ids) > 0:
            # A PLZ with a single loaded building gets it as its own grid; otherwise a street component
            # with one building is dropped. With one component, its building count is the PLZ's.
            if len(component_ids) > 1:
                single_building_plz = self.dbc.count_connected_buildings(vertices) == 1
                # Process multiple connected components
                for i, component_id in enumerate(component_ids):
                    # 1-D selection: int() of the 1-element rows of a 2-D selection is deprecated in NumPy.
                    related_vertices = vertices[component == component_id]
                    self._assign_component(related_vertices, i, keep_single_building=single_building_plz)
            else:
                # Process single connected component
                self._assign_component(vertices, keep_single_building=True)
        else:
            # No components found - issue warning
            warnings.warn("No connected components found in ways_tem table", stacklevel=2)

        # Verify clustering was successful for all buildings
        no_kmean_count = self.dbc.count_no_kmean_buildings()
        if no_kmean_count not in [0, None]:
            warnings.warn(f"K-means clustering issue: {no_kmean_count} buildings not assigned to clusters", stacklevel=2)

    def _assign_component(self, vertices, component_index=None, keep_single_building=False):
        """Helper method to process components to kcid groups.

        A component without loaded buildings is removed with its ways and transformers, and so is a
        component with one building unless ``keep_single_building`` (it is the only building of the PLZ).
        """
        conn_building_count = self.dbc.count_connected_buildings(vertices)

        if conn_building_count == 0 or (conn_building_count == 1 and not keep_single_building):
            # Remove isolated or empty components
            self.dbc.delete_ways(vertices)
            self.dbc.delete_transformers_from_buildings_tem(vertices)
            self.logger.debug("Empty/isolated component removed. Ways and transformers deleted from temporary tables.")
        elif conn_building_count > MAX_BUILDINGS_PER_KCID:
            # K-means applied to large components before the expensive KCID distance matrix is built.
            cluster_count = math.ceil(conn_building_count / MAX_BUILDINGS_PER_KCID)
            k_means = KMeans(n_clusters=cluster_count, random_state=K_MEANS_SEED, n_init="auto")
            (selected_vertices, coordinates) = self.dbc.get_connected_component_geometries(vertices)
            kcids = k_means.fit_predict(coordinates) + self.dbc.get_kcid_length() + 1
            self.dbc.update_kmeans_cluster_multiple(selected_vertices, kcids)
            log_msg = (
                f"Large component {component_index} clustered into {cluster_count} groups "
                f"(buildings={conn_building_count}, max_buildings_per_kcid={MAX_BUILDINGS_PER_KCID})"
                if component_index is not None
                else f"Large component clustered into {cluster_count} groups "
                     f"(buildings={conn_building_count}, max_buildings_per_kcid={MAX_BUILDINGS_PER_KCID})"
            )
            self.logger.debug(log_msg)
        else:
            # Allocate cluster id for connected component smaller than the building threshold
            self.dbc.update_kmeans_cluster(vertices)
