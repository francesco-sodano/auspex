from shared.sec_efts_connector import SecEftsConnector


class Sec13FConnector(SecEftsConnector):
    source_id = "sec_13f"
    schema_version = 2
    forms = "13F-HR,13F-HR/A"
    archive_profile = "13f"
