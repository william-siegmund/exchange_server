
from Llama_index.llama_rag import LlamaRag

"""
Flask app that acts as a middle-man between generated scripts
and a market exchange. Generated scripts call functions to this app
which will then perform operations on a Client class object to:
1) Send orders
2) Cancel orders
3) retrieve client orders
4) retrieve limit order book
"""

from flask import Flask, request, make_response, jsonify, render_template
from market_client.client import Client
import threading
import asyncio
import toml
from flask_cors import CORS

import logging
app = Flask(__name__)
CORS(app)


client = None
interpretor = None

def run_flask():
    """Start flask app"""
    with open('./market_client/config.toml', 'r') as f:
        config = toml.load(f)
    app.run(host="0.0.0.0", port=config['client']['flask_port'])

async def start(input_client: Client, openai_api_key):
    """Start client flask endpoint and connect to Market"""
    global client
    global interpretor
    # verify client class object is getting started
    if not input_client or not isinstance(input_client, Client):
        raise Exception(f"Cannot Start Non-Client object {input_client}")
    client = input_client
    interpretor = LlamaRag(openai_api_key)
    interpretor.configure_query_engine()
    print(client)
    # Run flask endpoint in separate thread to prevent it from blocking 
    # asyncio tcp connection to market
    t = threading.Thread(target=run_flask)
    t.start()
    await asyncio.gather(client.recver())
    
def send_to_market(request):
    """Send request to market exchange
    NOTE: The client uses asyncio to send tcp requests so we
          we must use asyncio.run() to call async methods from
          synchronous methods.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(client.send(request))
    else:
        loop.run_until_complete(client.send(request))

@app.route('/')
def home():
    return client.__str__()


@app.route('/prompt', methods=["POST", "GET"])
def prompt():
    data = request.get_json()
    prompt = data['prompt']
    
    confirmation_message = interpretor.send_query(prompt)
    return jsonify({"confirmation": confirmation_message})

@app.route('/execute', methods=["POST"])
def execute():

    interpretor.run_script()
    return jsonify({"status": "successful trade execution"})


@app.route('/place_order', methods=["POST"])
def place_order():
   
    # Parse order info
    order_info = request.json
    order_quantity = int(order_info.get("quantity"))
    order_price = int(order_info.get("price"))
    order_direction = order_info.get("direction")
    order_time = int(order_info.get("time"))

    # send order based on request 
    # https://discuss.python.org/t/calling-coroutines-from-sync-code/23027 thanks Sebastian :)
    ouch_order_request = client.place_order(order_quantity, order_price, order_direction, order_time)
    if ouch_order_request:
        send_to_market(ouch_order_request)
        placed_order_token = ouch_order_request['order_token'].decode()
        return {"order_token" : placed_order_token}
    return make_response(jsonify(error="Order Failed"),400)

@app.route('/cancel/<token>')
def cancel(token):
    cancel_info = request.json
    send_to_market(client.cancel_order(token, cancel_info.get("quantity_remaining")))

@app.route('/info')
def info():
    print(client.account_info())
    print(client.order_history)
    return {"account" : client.account_info(), "order_history" : client.order_history}

@app.route('/client_orders', methods=["GET"])
def get_client_orders():
    account_data = client.account_info()
    balance = account_data.get("balance")
    shares = account_data.get("owned_shares")
    orders = account_data.get("orders")
    
    orders_list = []
    
    for order_num, order_data in orders.items():
        orders_list.append({"order_num": order_num, "price": order_data["price"], "quantity": order_data["quantity"], "direction": order_data["direction"]})

    return jsonify({"balance": balance, "shares": shares, "orders": orders_list})

@app.route('/order_book', methods=["GET"])
def get_order_book():
    '''
    Returns order book
        format: {'bids': [{'price': 5, 'quantity': 3}], 
                 'asks': [{'price': 52, 'quantity': 8}]}
    
    '''
    book = client.order_book().get("book")

    return book

if __name__ == '__main__':
    app.run()

