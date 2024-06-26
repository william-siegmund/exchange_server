"""
Client class that communicates with a Continuous Double Auction exchange following the
ITCH message Protocol
""" 
import asyncio
from OuchServer.ouch_messages import OuchClientMessages, OuchServerMessages
import asyncio.streams
import configargparse
import logging as log
import uuid
from operator import itemgetter
from OuchServer.ouch_server import nanoseconds_since_midnight
from exchange.order_books import cda_book
from OuchServer.ouch_messages import OuchClientMessages, OuchServerMessages
import json
from exchange_logging.exchange_loggers import BookLogger, TransactionLogger, ClientStateLogger, ClientActionLogger

p = configargparse.ArgParser()
p.add('--delay', default=0, type=float, help="Delay in seconds between sending messages")
p.add('--debug', action='store_true')
p.add('--time_in_force', default=99999, type=int)
options, args = p.parse_known_args()



class Client:
    """A client that can communicate with a Continuous Double Auction(CDA)

    Attributes:
        reader: StreamReader instance that listens to CDA
        writer: StreamWriter instance that writes to CDA
        balance: An integer representing the amount of money a client has to spend
        owned_shares: An integer representing the amount of shares a client owns 
        id: uuid that separates the client instance from others
        orders: A dict describing the client's personal active orders
            where keys are order IDs and values is a dict {"price": order_price,"quantity": order_quantity, "direction": 'B' or 'S'}
        book_copy: A CDABook() that the client tries to replicate from the CDA exchange
        order_history: a list of clients' successful transactions in the format 
                       {"price" : traded_price, "quantity" : traded_quantity, "direction" : 'B' or 'S', "timestamp" : time} 
    """
    def __init__(self, balance=1000, starting_shares=50, host="127.0.0.1", port=8090):
        self.reader = None
        self.writer = None
        self.balance = balance
        self.owned_shares = starting_shares
        self.id = str(uuid.uuid4().hex).encode('ascii')
        self.orders = dict()
        self.book_copy = cda_book.CDABook()
        self.order_history = []
        self.host = host
        self.port = port
        # Market History Logging
        self.book_logger = BookLogger(log_filepath=f"market_client/market_logs/book_log.txt", logger_name="book_logger")
        self.transaction_logger = TransactionLogger(f"market_client/market_logs/transaction_log.txt", logger_name="transaction_logger")
        
        # Client Account History Logging
        self.state_logger = ClientStateLogger(f"market_client/market_logs/state_log.txt", logger_name="state_logger")
        self.state_logger.update_log(self.account_info(), timestamp=nanoseconds_since_midnight())

    def __str__(self):
        return (f"Account Information\n"
                f"Balance: {self.balance}\n"
                f"Owned_shares: {self.owned_shares}\n"
                f"Orders: {self.orders}\n"
                f"Order History: {self.order_history}\n")

    def print_active_orders(self):
        """Display active client orders"""
        print(f'Your active orders')
        count = 1
        for order_id in self.orders:
            print(f"ID: {count}, {self.orders[order_id]['quantity']} shares @ ${self.orders[order_id]['price']}")
            count += 1
        
    def account_info(self):
        return {"id" : self.id.decode(), "balance" : self.balance, "orders" : self.orders, "owned_shares" : self.owned_shares}
    
    def order_book(self):
        return {"book": self.book_copy.as_json()}

    def _update_account(self, cost_per_share, num_shares, direction, timestamp):
        """update the state of account upon successful trade
        Args:
            cost_per_share: int specifying the value of 1 share
            num_shares: int specifying the quantity of shares
            direction: str specifying how to update the account
            timestamp: int specifying the time of the update
        """
        if direction == 'B':
            self.owned_shares += num_shares
        else:
            self.balance += (num_shares * cost_per_share)
        
        # Update Client StateLog
        self.state_logger.update_log(client_info = self.account_info(), timestamp=timestamp)

    def _update_active_orders(self, execution: OuchServerMessages.Executed):
        """Update client account based on details of the original order and
        the execution

        Args:
            execution: an OuchServerMessage that includes details of a trade
        """
        sold_shares = execution['executed_shares']
        price_per_share = execution['execution_price']
        order_id = execution['order_token'].decode()
        timestamp = execution['timestamp']
        # Get details of the original order from client
        proposed_price, desired_shares, direction, time_in_force, order_timestamp = self.orders[order_id].values()
        self._update_account(price_per_share, sold_shares, direction, timestamp)
        print(self.orders[order_id], flush=True)
        # Check that order was completely fulfilled
        if sold_shares == desired_shares:
            self.orders.pop(order_id)
        else:
            self.orders[order_id] = {
                "price" : proposed_price,
                "quantity" : desired_shares - sold_shares,
                "direction" : direction,
                "time_in_force": time_in_force,
                "timestamp": order_timestamp
            }   
    
    def _can_afford(self, cost_per_share, num_shares):
        """Can client create the order with their current balance?
        Args:
            cost_per_share: an int representing the price per share
            num_shares: an int representing the quantity of shares to buy
        Returns:
            A bool that specifies whether the client has the funds to make a buy
            num_shares at cost_per_share
        """
        return self.balance >= (cost_per_share * num_shares)
    
    async def recv(self):
        """Convert response from bytes into ouch response format
        
        Returns: OuchServer.ouch_message object in the format of one of the many formats
        (found in Lines past 134 in OuchServer\ouch_messages.py):
        """
        try:
            header = (await self.reader.readexactly(1))
        except asyncio.IncompleteReadError:
            log.error('connection terminated without response')
            return None, None
        message_type = OuchServerMessages.lookup_by_header_bytes(header)
        try:
            payload = (await self.reader.readexactly(message_type.payload_size))
        except asyncio.IncompleteReadError as err:
            log.error('Connection terminated mid-packet!')
            return None, None
        log.debug('Received Ouch payload as binary: %r', payload)
        log.debug('bytes: %r', list(payload))

        response_msg = message_type.from_bytes(payload, header=False)
        return response_msg, message_type

    async def recver(self):
        """Listener to all broadcasts sent from the exchange server"""
        if self.reader is None or self.writer is None:
            try:
                reader, writer = await asyncio.streams.open_connection(
                self.host, 
                self.port)
                self.reader = reader
                self.writer = writer
            except ConnectionRefusedError:
                print("Exchange not Started!", flush=True)
                return

        while not self.reader.at_eof():
            response, message_type = await self.recv()
            if response is None or message_type is None:
                continue
            match message_type:

                # Order book has new best bid or ask(offer)
                # Ex:(ignoring quantity) buy {$2, $1} sell {}, if a client made a sell order of $1 then
                # the order book will update to buy {$1} sell {}, which means the new best bid is $1 and best ask is 0 
                case OuchServerMessages.BestBidAndOffer:
                    print("new best buy offer: ", response)
                case OuchServerMessages.Executed:
                    # Trade has been made
                    print(f"{response['order_token']} executed {response['executed_shares']} shares@ ${response['execution_price']}")
              
                    order_id = response['order_token'].decode()
                    if order_id in self.orders:
                        transaction_data = {"price" : response['execution_price'], "quantity" : response["executed_shares"], "direction" : self.orders[order_id]['direction'], "timestamp" : response['timestamp']}
                        self.order_history.append(transaction_data)
                        self._update_active_orders(response)
                    
                    # Update Book Log & Transaction Log
                    self.book_logger.update_log(book=self.book_copy, timestamp=response['timestamp'])
                    self.transaction_logger.update_log(transaction=response, timestamp=response['timestamp'])

                # update client local_book 
                case OuchServerMessages.Accepted:
                    decoded_token = response['order_token'].decode() 
                    print("The server Accepted order ", decoded_token)
                    # check if OUR order was accepted and add a timestamp
                    if decoded_token in self.orders:
                        self.orders[decoded_token]["timestamp"] = response["timestamp"]
                    
                    time_in_force = response['time_in_force']
                    enter_into_book = True if time_in_force > 0 else False
                    enter_order_func = self.book_copy.enter_buy if response['buy_sell_indicator'] == b'B' else self.book_copy.enter_sell
                    enter_order_func(
                        response['order_token'],
                        response['price'],
                        response['shares'],
                        enter_into_book
                    )
                    # Update Book Log
                    self.book_logger.update_log(book=self.book_copy, timestamp=response['timestamp'])

                # update client local_book
                case OuchServerMessages.Canceled:
                    print("The server canceled order", response['order_token'])
                    quantity = 0
                    cancelled_order_id = response['order_token'].decode()
                    if cancelled_order_id in self.orders:
                        price, quantity, direction, time_in_force, timestamp = self.orders[cancelled_order_id].values()
                        # Update order based on remaining shares
                        self.orders[cancelled_order_id] = {"price" : price, "quantity" : quantity - response['decrement_shares'], "direction" : direction, "time_in_force": time_in_force, "timestamp" : timestamp}
                        if direction == 'B':
                            direction = 'S'
                        else:
                            direction = 'B'
                      
                        self._update_account(price, response['decrement_shares'], direction, response['timestamp'])
                        # Remove order if all shares were canceled
                        if quantity == response['decrement_shares']:
                            self.orders.pop(cancelled_order_id)

                    # Cancel order from book_copy
                    # NOTE: The quantity canceled is how many shares should remain
                    self.book_copy.cancel_order(
                        response['order_token'],
                        response['price'],
                        quantity - response['decrement_shares'] if quantity else 0,
                        response['buy_sell_indicator']
                    )

                    # Update Book Log
                    self.book_logger.update_log(book=self.book_copy, timestamp=response['timestamp'])

                case _:
                    print(response.header)
            await asyncio.sleep(0)
        

    def _valid_order_input(self, quantity=None, price=None, direction=None, order_token=None, time_in_force=None):
        """Determine if valid parameters were entered
        Args:
            quantity: an expected int 
            price: an expected int 
            direction: an expected str
            order_token: an expected int
            time_in_force: an expected int
        Returns:
            False, if args are invalid
            tuple containing args in their respective formats
        """
        try:
            if quantity is not None:
                quantity = int(quantity)
                if quantity < 0:
                    return False
            
            if price is not None:
                price = int(price)
                if price < 1:
                    return False

            if direction is not None:
                if direction not in {'B', 'S'}:
                    return False

            if order_token is not None:
                order_token = int(order_token)
                if order_token < 1:
                    return False
            
            if time_in_force is not None:
                time_in_force = int(time_in_force)
                if time_in_force == 0:
                    time_in_force = 10
                if time_in_force < 1 or time_in_force > 99998:
                    return False

            return (quantity, price, direction, order_token, time_in_force)
        except ValueError:
            return False

    async def send(self, request):
        """Send Ouch message to server
        
        Args:
            request: an OuchClientMessages object representing a buy/sell or cancel order
         
        """
        if self.reader is None or self.writer is None:
            try:
                reader, writer = await asyncio.streams.open_connection(
                self.host, 
                self.port)
                self.reader = reader
                self.writer = writer
            except ConnectionRefusedError:
                print(f"Could not connect to {self.host}:{self.port}")
                return
        # Ignore None requests
        if not request:
            print("Invalid order")
            return
        self.writer.write(bytes(request))
        await self.writer.drain()

    def place_order(self, quantity, price, direction, time_in_force=None):
        """Make an Ouch Limit order
        Args:
            quantity: an int representing number of shares for the order
            price: an int representing price to buy shares at
            direction: str that specifies whether the order is a BUY or SELL
            time_in_force: int that specifies duration(in seconds) that order should last

        Returns:
            None if order contains improper arguments
            Else return OuchClientMessages.EnterOrder
        Note:
            if no time_in_force is specified, order will act like a Market Order instead of Limit Order
        """
        res = self._valid_order_input(quantity=quantity, price=price, direction=direction, time_in_force=time_in_force)
        if not res:
            return None

        quantity, price, direction, order_token, time_in_force = res
        # Client can only buy what they can afford
        if direction == 'B' and self._can_afford(price, quantity):
            self.balance -= price * quantity
        # Client can only sell at most amount of their owned_shares
        elif direction == 'S' and quantity <= self.owned_shares:
            self.owned_shares -= quantity
        elif not time_in_force:
            return None
        else:
            return None
        # Generate unique token
        order_token=str(uuid.uuid4().hex).encode('ascii')
        
        order_request = OuchClientMessages.EnterOrder(
            order_token=order_token,
            buy_sell_indicator=b'B' if direction == 'B' else b'S',
            shares=quantity,
            stock=b'FIRST',#b'AMAZGOOG',
            price=price,
            time_in_force=time_in_force if time_in_force else options.time_in_force,
            firm=bytes(self.id),
            display=b'N',
            capacity=b'O',
            intermarket_sweep_eligibility=b'N',
            minimum_quantity=1,
            cross_type=b'N',
            customer_type=b' ',
            midpoint_peg=b' '
        )

        # update local orders
        self.orders[order_token.decode()] = {"price" : price, "quantity" : quantity, "direction" : direction, "time_in_force": time_in_force}

        return order_request

    def cancel_order(self, order_token, quantity_remaining):
        """Convert user input into cancel order 
        Args:
            order_token: an int representing order id
            quantity_remaining: an int representing how many shares should remain part of the order
        
        Returns:
            None if order contains improper arguments
            Else return OuchClientMessages.CancelOrder
        Note:
            Cancel all or part of an order. quantity_remaining refers to the desired remaining shares to be executed: 
            if it is 0, the order is fully cancelled, otherwise an order of quantity_remaining remains.
        
        Ex: cancel_order 3 shares for a buy order of 4 shares for $6 will adjust the buy order to 3 shares for $6
        Ex: cancel_order 0 shares for a buy order of 4 shares for $6 will adjust the buy order to 0 shares for $6.
            This will remove the order from the exchange and refund the client.

        """
        res = self._valid_order_input(order_token=order_token, quantity=quantity_remaining)
        if not res:
            return None
        quantity_remaining, price, direction, order_token, time_in_force = res
        count = 1  
        # Try to match order_token with order_id
        for order_id in self.orders:
            if count == order_token:
                order_token = order_id
                break
            count +=1 

        cancel_request = OuchClientMessages.CancelOrder(
            order_token=str(order_token).encode('ASCII'), 
            shares=quantity_remaining,
        )

        return cancel_request



    async def sender(self):
        """
        Currently, this is where all Client send operations are called.

        This is only used when using the --mode dev in run_market_client.py
        """
        if self.reader is None:
            reader, writer = await asyncio.streams.open_connection(
            self.host, 
            self.port)
            if not self.reader:
                self.reader = reader
                self.writer = writer
        while True:
            print(self)
            print(self.book_copy.as_json())
            cmd = input("Make a command order(O) or cancel(C): ")
            await asyncio.sleep(0.5)
            if cmd == "O":
                user_quantity = input("Enter number of shares: ")
                user_price = input("Enter share price: ")
                user_direction = input("Buy(B) or Sell(S): ")
                user_time_in_force = input("Order Lifetime: ")
                await self.send(self.place_order(user_quantity, user_price, user_direction, user_time_in_force))

            elif cmd == "C":
                if not self.orders:
                    print("No active orders!")
                    continue         
                self.print_active_orders()
                user_order_token = input("ID of order to cancel: ")
                user_shares_removed = input("How many shares should remain?: ")
                await self.send(self.cancel_order(user_order_token, user_shares_removed))
            else:
                print(f"Invalid command {cmd}")

            # sleeping will allow the client.recver() method to process
            await asyncio.sleep(0.5)  

    async def runner(self):
         """run client internally
         NOTE: This is used in dev_run_multiple_clients.py"""
         await asyncio.gather(self.sender(), self.recver())
#----------------------------DEBUG------------------------
async def main():
    log.basicConfig(level=log.INFO if not options.debug else log.DEBUG)
    log.debug(options)
    
    # creates a client and connects to our server
    client = Client()

    await asyncio.gather(client.sender(), client.recver())

if __name__ == '__main__':   
    asyncio.run(main())