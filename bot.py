import os
from flask import Flask

app = Flask(__name__)

@app.route('/')
def hello():
    return "Bot is running!", 200

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    print(f"Starting Flask server on port {port}")
    app.run(host='0.0.0.0', port=port)
