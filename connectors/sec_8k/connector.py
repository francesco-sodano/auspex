from shared.sec_efts_connector import SecEftsConnector


class Sec8KConnector(SecEftsConnector):
    source_id = "sec_8k"
    schema_version = 2
    forms = "8-K,8-K/A"
    archive_profile = "8k"
