from app import create_app

app = create_app()

if __name__ == "__main__":
    # Keep Flask's interactive debugger for local errors, but never enable
    # Werkzeug's stat reloader. The reloader creates a second process and can
    # tear down the Windows server socket while watched files change.
    app.run(
        host="0.0.0.0",
        port=app.config["PORT"],
        debug=app.config["DEBUG"],
        use_reloader=False,
    )
