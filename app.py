from flask import Flask

server = Flask(__name__)

@server.route("/")
def get_webpage():
    return server.send_static_file("index.html")

if __name__ == "__main__":
    server.run(host='0.0.0.0')
