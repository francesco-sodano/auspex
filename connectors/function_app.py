import azure.functions as func

from prices_eod.blueprint import bp as prices_eod_bp
from sec_form4.blueprint import bp as sec_form4_bp

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
app.register_blueprint(sec_form4_bp)
app.register_blueprint(prices_eod_bp)
