"""SEC EDGAR Form 4 connector — insider transaction filings."""
from shared.sec_efts_connector import SecEftsConnector


class SecForm4Connector(SecEftsConnector):
    source_id = "sec_form4"
    forms = "4,4/A"
