from flask import jsonify


def error_response(message, code):
    """Consistent {"error": {"code", "message"}} JSON shape for every API error."""
    return jsonify({"error": {"code": code, "message": message}}), code
