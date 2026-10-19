import os
import sys

try:
    from waitress import serve
    from app import create_app

    app = create_app()
    print("=========================================================")
    print(" Cellar & Crumb Operations ERP — Production WSGI Server  ")
    print(" Running securely on: http://127.0.0.1:5000               ")
    print(" Network / Branch URL: http://0.0.0.0:5000               ")
    print(" Press Ctrl+C to stop server                             ")
    print("=========================================================")
    serve(app, host='0.0.0.0', port=5000, threads=8)
except ImportError:
    from app import create_app
    app = create_app()
    print("Starting server...")
    app.run(host='0.0.0.0', port=5000, debug=False)
