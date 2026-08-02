from shared.sec_efts_connector import SecEftsConnector


class SecS1Connector(SecEftsConnector):
    source_id = "sec_s1"
    schema_version = 2
    forms = "S-1,S-1/A,424B4,424B5"
    archive_profile = "s1"
