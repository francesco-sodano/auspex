from shared.sec_efts_connector import SecEftsConnector


class Sec13DgConnector(SecEftsConnector):
    source_id = "sec_13dg"
    forms = "SC 13D,SC 13D/A,SC 13G,SC 13G/A"
