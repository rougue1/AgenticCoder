import pytest
from ..create_app import create_app
from ..extensions import db

def test_create_app_development(app):
    assert app.config['DEBUG'] is True
    assert app.config['TESTING'] is False

def test_create_app_testing():
    app = create_app("testing")
    assert app.config['DEBUG'] is False
    assert app.config['TESTING'] is True

def test_api_blueprint_registered(client):
    response = client.get('/api/health')
    assert response.status_code == 200
    assert response.json == {'status': 'healthy'}

def test_error_handlers_404(client):
    response = client.get('/nonexistent')
    assert response.status_code == 404
    assert response.json == {'error': 'Not Found'}

def test_error_handlers_500(client, db_session):
    @client.application.route('/trigger-500')
    def trigger_500():
        raise Exception("Triggered 500 Error")
    
    response = client.get('/trigger-500')
    assert response.status_code == 500
    assert response.json == {'error': 'Internal Server Error'}