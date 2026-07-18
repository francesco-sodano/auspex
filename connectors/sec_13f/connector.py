from shared.sec_efts_connector import SecEftsConnector


class Sec13FConnector(SecEftsConnector):
    source_id = "sec_13f"
    forms = "13F-HR,13F-HR/A"
