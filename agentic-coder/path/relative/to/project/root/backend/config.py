from flask import Flask

app = Flask(__name__)
app.config.from_object('backend.config.TestingConfig')

class TestingConfig(object):
    TESTING = True