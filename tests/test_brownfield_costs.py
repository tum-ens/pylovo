"""get_consumer_to_transformer_df without consumers: an empty table instead of an ambiguous pgRouting call (no database)."""

from pylovo.database.clustering_mixin import ClusteringMixin


class Cursor:
    def __init__(self):
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append(query)

    def fetchall(self):
        return []


def test_a_kcid_without_consumers_gives_no_costs():
    client = ClusteringMixin.__new__(ClusteringMixin)
    client.cur, client.conn = Cursor(), None
    costs = client.get_consumer_to_transformer_df(2, [101, 102])
    assert costs.empty and list(costs.columns) == ["start_vid", "end_vid", "agg_cost"]
    assert len(client.cur.queries) == 1   # only the consumer query, no pgr_dijkstraCost
