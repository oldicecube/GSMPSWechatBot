from .flower import FlowerSource


class GrassSource(FlowerSource):
    source_id = "grass"
    name = "野草"
    default_base_url = "http://97.64.37.235/grass/v1"
