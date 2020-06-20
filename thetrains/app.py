# -*- coding: utf-8 -*-

"""Module to create Flask and Dash applications."""

from decouple import config
from flask import Flask
from dash import Dash
import dash_bootstrap_components as dbc

from common.config import config_dict
from common.mongo import Mongo
from thetrains.graph import Graph


def create_flask():
    """Create the Flask instance for this application.

    Returns:
        flask.Flask: flask application
    """
    server = Flask(__package__)

    # load default settings
    conf = config_dict[config("ENV", cast=str, default="local")]
    server.config.from_object(conf)

    return server


def create_dash(server):
    """Create the Dash instance for this application.

    Args:
        server (flask.Flask): flask application
    Returns:
        dash.Dash: dash application
    """
    app = Dash(
        name=__package__,
        server=server,
        suppress_callback_exceptions=True,
        external_stylesheets=[dbc.themes.LUX],
    )

    # Initialise logging
    conf = config_dict[config("ENV", cast=str, default="local")]
    conf.init_logging(app.logger)
    server.logger.removeHandler(app.logger.handlers[0])

    # Initialise the mongo database
    app.mongo = Mongo(app.logger, app.server.config["MG_URI"])

    # Initialise the graph
    # TODO: periodically update the graph with any new data
    app.graph = Graph(app.logger, app.mongo)

    # Update the Flask config a default "TITLE" and then with any new Dash
    # configuration parameters that might have been updated so that we can
    # access Dash config easily from anywhere in the project with Flask's
    # 'current_app'
    server.config.setdefault("TITLE", "Dash")

    # Set the app name
    app.title = "thetrains"

    return app


# Create the Flask instance
server = create_flask()

# Create the Dash instance
app = create_dash(server)
