"""Continuous Double Auction Exchange implementation that handles buy/sell, cancelation, and
execution of Limit Orders. This exchange Originally was used to support the Nasdaq Ouch protocol.
It has been refactored to support the Nasdaq ITCH protocol.

NOTE: Market Orders are not supported
"""

import asyncio
import asyncio.streams
import logging as log
import itertools
from functools import partial
from collections import deque

from OuchServer.ouch_messages import OuchClientMessages, OuchServerMessages
from OuchServer.ouch_server import nanoseconds_since_midnight

from exchange.order_store import OrderStore

from exchange_logging.exchange_loggers import BookLogger, TransactionLogger, ClientActionLogger, PLACE_LIMIT_ORDER_ACTION, CANCEL_LIMIT_ORDER_ACTION

class Exchange:
    def __init__(self, order_book, order_reply, loop, message_broadcast = None, book_log='book_log.txt', transaction_log='transaction_log.txt', action_log='action_log.txt'):
        """
        order_store: tracks orders submitted to the exchange and their status(expiration)
        order_book: Limit Order book that self updates and matches buy and sell orders
        order_reply: post office reply function, takes in 
                message 
                original order
                context
            and does whatever we need to get that info back to order sender
        message_broadcast: 
        outgoing_broadcast_messages: 
        handlers: A dict of methods to handle corresponding client message
        """
        self.order_store = OrderStore()
        self.order_book = order_book
        self.order_reply = order_reply
        self.message_broadcast = message_broadcast
        self.next_match_number = 0
        self.loop = loop
        self.outgoing_messages = deque()
        self.order_ref_numbers = itertools.count(1, 2)  # odds    
        self.outgoing_broadcast_messages = deque() 
        self.handlers = { 
            OuchClientMessages.EnterOrder: self.enter_order_atomic,
            OuchClientMessages.ReplaceOrder: self.replace_order_atomic,
            OuchClientMessages.CancelOrder: self.cancel_order_atomic,
            OuchClientMessages.SystemStart: self.system_start_atomic,
            OuchClientMessages.ModifyOrder: None}

        # BOOK HISTORY LOG
        self.book_log_file = book_log
        self.book_logger = BookLogger(log_filepath=f"exchange/market_logs/{book_log}", logger_name="book_logger")

        # TRANSACTION HISTORY LOG
        self.transaction_log_file = transaction_log
        self.transaction_logger = TransactionLogger(log_filepath=f"exchange/market_logs/{transaction_log}", logger_name="transaction_logger")
    
        # CLIENT ACTION HISTORY LOG
        self.action_log_file = action_log
        self.action_logger = ClientActionLogger(f"exchange/market_logs/{action_log}", logger_name="action_logger")


    def system_start_atomic(self, system_event_message, timestamp):
        """Clear past data of exchange to simulate the creation of a new exchange"""  
        self.order_store.clear_order_store()
        self.order_book.reset_book()
        m = OuchServerMessages.SystemEvent(event_code=b'S', timestamp=timestamp)
        m.meta = system_event_message.meta
        self.outgoing_messages.append(m)

    def accepted_from_enter(self, enter_order_message, timestamp, order_reference_number, order_state=b'L', bbo_weight_indicator=b' '):
        """Create Accept server response from a buy/sell order message
        
        Args:
            enter_order_message: OuchClientMessage representing a buy/sell order
            timestamp: Time(in seconds) the client's order was entered
            order_reference_number: A int for the order number in context of the entire exchange
            order_state: A byte string representing whether the order is Limit order(b'L')
        Returns:
            OuchServerMessages.Accepted message containing information about the clients' order and Exchange internal information
        """
        m = OuchServerMessages.Accepted(
            timestamp=timestamp,
            order_reference_number=order_reference_number, 
            order_state=order_state,
            bbo_weight_indicator=bbo_weight_indicator,
            order_token=enter_order_message['order_token'],
            buy_sell_indicator=enter_order_message['buy_sell_indicator'],
            shares=enter_order_message['shares'],
            stock=enter_order_message['stock'],
            price=enter_order_message['price'],
            time_in_force=enter_order_message['time_in_force'],
            firm=enter_order_message['firm'],
            display=enter_order_message['display'],
            capacity=enter_order_message['capacity'],
            intermarket_sweep_eligibility=enter_order_message['intermarket_sweep_eligibility'],
            minimum_quantity=enter_order_message['minimum_quantity'],
            cross_type=enter_order_message['cross_type'],
            midpoint_peg=enter_order_message['midpoint_peg'])
        m.meta = enter_order_message.meta
        return m

    def cancel_order_from_enter_order(self, enter_order_message, reason = b'U'):
        """Create CancelOrder on behalf of client when order exceeds time_in_force
        Args:
            enter_order_message: Original OuchClientMessages.Order that is used to compose CancelOrder
        Returns:
            OuchCLientMessages.Cancel 
        """
        m = OuchClientMessages.CancelOrder(
            order_token = enter_order_message['order_token'],
            shares = 0
            )
        m.meta = enter_order_message.meta
        return m

    # NOTE: Unused
    def cancel_order_from_replace_order(self, replace_order_message, reason = b'U'):
        m = OuchClientMessages.CancelOrder(
            #order_token = replace_order_message['replacement_order_token'],
            order_token = replace_order_message['existing_order_token'],
            shares = 0
            )
        m.meta = replace_order_message.meta
        return m
    
    def order_cancelled_from_cancel(self, original_enter_message, timestamp, amount_canceled, reason=b'U',order_token = None):
        """Create CancelOrder when Clients' request to cancel an order
        Args:
            original_enter_message: OuchClientMessage representing the buy/sell order the client wants to cancel
            timestamp: Time(in seconds) of when the order was canceled
            amount_canceled: The amount of shares to remain
            order_token: The unique token representing the clients' order
        Returns:
            OuchCLientMessages.Cancel 
        """
        order_token = original_enter_message['order_token'] if order_token is None else order_token
        m = OuchServerMessages.Canceled(timestamp = timestamp,
                            order_token = order_token,
                            decrement_shares = amount_canceled,
                            reason = reason,
                            midpoint_peg = original_enter_message['midpoint_peg'],
                            price = original_enter_message['price'],
                            buy_sell_indicator = original_enter_message['buy_sell_indicator'])
        m.meta = original_enter_message.meta
        return m
    
    def best_quote_update(self, order_message, new_bbo, timestamp):
        m = OuchServerMessages.BestBidAndOffer(timestamp=timestamp, stock=b'AMAZGOOG',
            best_bid=new_bbo.best_bid, volume_at_best_bid=new_bbo.volume_at_best_bid,
            best_ask=new_bbo.best_ask, volume_at_best_ask=new_bbo.volume_at_best_ask,
            next_bid=new_bbo.next_bid, next_ask=new_bbo.next_ask 
        )
        m.meta = order_message.meta
        return m

    def process_cross(self, id, fulfilling_order_id, price, volume, timestamp, liquidity_flag = b'?'):
        """Create response msgs for clients involved in a trade(when orders cross)
        Args:
            id: Order_token for newly entered order
            fulfilling_order_id: Order_token for order already in the exchange
            price: An int representing price overlap
            volume: An int representing the amount of shares that will be exchanged
            timestamp: Time(in seconds) of initial transaction
        Returns:
            A list of size 2 containing OuchServerMesssages for each client in the trade
        """
        log.info('Orders (%s, %s) crossed at price %s, volume %s', id, fulfilling_order_id, price, volume)
        order_message = self.order_store.orders[id].first_message
        fulfilling_order_message = self.order_store.orders[fulfilling_order_id].first_message
        log.info('incoming order message: %s, fullfilling order message: %s',order_message,fulfilling_order_message)
        match_number = self.next_match_number
        self.next_match_number += 1
        original_enter_message = self.order_store.orders[id].original_enter_message
        r1 = OuchServerMessages.Executed(
                timestamp = timestamp,
                order_token = id,
                executed_shares = volume,
                execution_price = price,
                liquidity_flag = liquidity_flag,
                match_number = match_number,
                midpoint_peg = original_enter_message['midpoint_peg']
                )
        r1.meta = order_message.meta
        self.order_store.add_to_order(r1['order_token'], r1)
        fulfilling_original_enter_message = self.order_store.orders[fulfilling_order_id].original_enter_message
        r2 = OuchServerMessages.Executed(
                timestamp = timestamp,
                order_token = fulfilling_order_id,
                executed_shares = volume,
                execution_price = price,
                liquidity_flag = liquidity_flag,
                match_number = match_number,
                midpoint_peg = fulfilling_original_enter_message['midpoint_peg']
                )
        r2.meta = fulfilling_order_message.meta
        self.order_store.add_to_order(r2['order_token'], r2)
        return [r1, r2]

    def enter_order_atomic(self, enter_order_message, timestamp, executed_quantity = 0):
        """Add an order to the exchange
        Args:
            enter_order_message: OuchMessage.EnterOrder
            timestamp: int that represents time(in seconds) of when order was made
            executed_quantity: int specifying amount of shares to sell/buy
        """
        order_stored = self.order_store.store_order( 
            id = enter_order_message['order_token'], 
            message = enter_order_message, 
            executed_quantity = executed_quantity)
        if not order_stored:
            log.info('Order already stored with id %s, order ignored', enter_order_message['order_token'])
            m = OuchServerMessages.Rejected(
                    timestamp = timestamp,
                    order_token = enter_order_message['order_token'],
                    reason = b'RepeatID',
                    price = enter_order_message['price'],
                    shares = enter_order_message['shares']
                )
            m.meta = enter_order_message.meta
            self.outgoing_messages.append(m)
            return 
        else:
            time_in_force = enter_order_message['time_in_force']
            enter_into_book = True if time_in_force > 0 else False    
            #schedule a cancellation at some point in the future
            if time_in_force > 0 and time_in_force < 99998:     
                cancel_order_message = self.cancel_order_from_enter_order( enter_order_message )
                self.loop.call_later(time_in_force, partial(self.cancel_order_atomic, cancel_order_message, timestamp))
            
            enter_order_func = self.order_book.enter_buy if enter_order_message['buy_sell_indicator'] == b'B' else self.order_book.enter_sell
            (crossed_orders, entered_order, new_bbo) = enter_order_func(
                    enter_order_message['order_token'],
                    enter_order_message['price'],
                    enter_order_message['shares'],
                    enter_into_book)
            log.info("Resulting book: %s", self.order_book)
            m=self.accepted_from_enter(enter_order_message, 
                order_reference_number=next(self.order_ref_numbers),
                timestamp=timestamp)
            self.order_store.add_to_order(m['order_token'], m)
            #self.outgoing_messages.append(m)
            self.outgoing_broadcast_messages.append(m)
            # Prepare messages to broadcast all clients
            # This includes when the best buy offer changes
            cross_messages = [m for ((id, fulfilling_order_id), price, volume) in crossed_orders 
                                for m in self.process_cross(id, fulfilling_order_id, price, volume, timestamp=timestamp)]
            #self.outgoing_messages.extend(cross_messages)
            self.outgoing_broadcast_messages.extend(cross_messages)
            # if cross_messages:
            #     self.outgoing_broadcast_messages.append(cross_messages[1])
            if new_bbo:
                bbo_message = self.best_quote_update(enter_order_message, new_bbo, timestamp)
                self.outgoing_broadcast_messages.append(bbo_message)
            
            # Update Client Action Log
            self.action_logger.update_log(action_type=PLACE_LIMIT_ORDER_ACTION, client_action_msg=enter_order_message, timestamp=timestamp)


    def cancel_order_atomic(self, cancel_order_message, timestamp, reason=b'U'):
        """Cancel an order
        Args:
            cancel_order_message: OuchClientMessages.CancelOrder containing order information needed to cancel
            timestamp: time, in seconds, that order was cancelled
        """
        store_entry = self.order_store.orders.get(cancel_order_message['order_token'])
        if store_entry is None:
            log.info(f"No such order to cancel, ignored. Token to cancel: {cancel_order_message['order_token']}")
        else:
            original_enter_message = store_entry.original_enter_message
            cancelled_orders, new_bbo = self.order_book.cancel_order(
                id = cancel_order_message['order_token'],
                price = store_entry.first_message['price'],
                volume = cancel_order_message['shares'],
                buy_sell_indicator = store_entry.original_enter_message['buy_sell_indicator'])
           
           
            # Remove order entry if all shares were cancelled
            if cancel_order_message['shares'] == 0:
                 self.order_store.orders.pop(cancel_order_message['order_token'], None)
            # Order was traded or canceled before it expired
            if not cancelled_orders and not new_bbo:
                return
            # Create and broadcast cancel message(s)
            cancel_messages = [ self.order_cancelled_from_cancel(original_enter_message, timestamp, amount_canceled, reason,order_token= cancel_order_message['order_token'])
                        for (id, amount_canceled) in cancelled_orders ]
            self.outgoing_broadcast_messages.extend(cancel_messages) 
            log.info("Resulting book: %s", self.order_book)
            if new_bbo:
                bbo_message = self.best_quote_update(cancel_order_message, new_bbo, timestamp)
                self.outgoing_broadcast_messages.append(bbo_message)
            
            # Update Client Action Log
            client_action_data = cancel_order_message 
            self.action_logger.update_log(action_type=CANCEL_LIMIT_ORDER_ACTION, client_action_msg=client_action_data, timestamp=timestamp)

            # Broadcast cancel message(s)
            loop = asyncio.get_event_loop()
            loop.call_soon_threadsafe(loop.create_task, self.send_outgoing_broadcast_messages())

    # """
    # NASDAQ may respond to the Replace Order Message in several ways:
    #     1) If the order for the existing Order Token is no longer live or if the replacement Order
    #         Token was already used, the replacement will be silently ignored.
    #     2) If the order for the existing Order Token is live but the details of the replace are
    #         invalid (e.g.: new Shares >= 1,000,000), a Canceled Order Message will take the
    #         existing order out of the book. The replacement Order Token will not be consumed,
    #         and may be reused in this case.
    #     3) If the order for the existing Order Token is live but the existing order cannot be
    #         canceled (e.g.: the existing Order is a cross order in the late period), there will be a
    #         Reject Message. This reject message denotes that no change has occurred to the
    #         existing order; the existing order remains fully intact with its original instructions.
    #         The Reject Message consumes the replacement Order Token, so the replacement
    #         Order Token may not be reused.
    #     4) If the order for the existing Order Token is live and can be replaced, you will receive
    #         either a Replaced Message or an Atomically Replaced and Canceled Message.
    # """
    # NOTE: CURRENTLY UNUSED
    def replace_order_atomic(self, replace_order_message, timestamp):
        if replace_order_message['existing_order_token'] not in self.order_store.orders:
            log.debug('Existing token %s unknown, siliently ignoring', replace_order_message['existing_order_token'])
            return []
        elif replace_order_message['replacement_order_token'] in self.order_store.orders:
            log.debug('Replacement token %s unknown, siliently ignoring', replace_order_message['existing_order_token'])
            return []
        else:
            store_entry = self.order_store.orders[replace_order_message['existing_order_token']]
            log.debug('store_entry: %s', store_entry)
            cancelled_orders, new_bbo_post_cancel = self.order_book.cancel_order(
                id = replace_order_message['existing_order_token'],
                price = store_entry.first_message['price'],
                volume = 0,
                buy_sell_indicator = store_entry.original_enter_message['buy_sell_indicator'])  # Fully cancel
            
            if len(cancelled_orders)==0:
                log.debug('No orders cancelled, siliently ignoring')
                return []
            else:
                (id_cancelled, amount_cancelled) = cancelled_orders[0]
                original_enter_message = store_entry.original_enter_message
                first_message = store_entry.first_message
                shares_diff = replace_order_message['shares'] - first_message['shares'] 
                liable_shares = max(0, amount_cancelled + shares_diff )
                if liable_shares == 0:
                    log.debug('No remaining liable shares on the book to replace')
                    #send cancel
                else:
                    self.order_store.store_order(
                            id = replace_order_message['replacement_order_token'], 
                            message = replace_order_message,
                            original_enter_message = original_enter_message)
                    time_in_force = replace_order_message['time_in_force']
                    enter_into_book = True if time_in_force > 0 else False    
                    if time_in_force > 0 and time_in_force < 99998:     #schedule a cancellation at some point in the future
                        cancel_order_message = self.cancel_order_from_replace_order( replace_order_message )
                        self.loop.call_later(time_in_force, partial(self.cancel_order_atomic, cancel_order_message, timestamp))
                    
                    enter_order_func = self.order_book.enter_buy if original_enter_message['buy_sell_indicator'] == b'B' else self.order_book.enter_sell
                    crossed_orders, entered_order, new_bbo_post_enter = enter_order_func(
                            replace_order_message['replacement_order_token'],
                            replace_order_message['price'],
                            liable_shares,
                            enter_into_book)

                    r = OuchServerMessages.Replaced(
                            timestamp=timestamp,
                            replacement_order_token = replace_order_message['replacement_order_token'],
                            buy_sell_indicator=original_enter_message['buy_sell_indicator'],
                            shares=liable_shares,
                            stock=original_enter_message['stock'],
                            price=replace_order_message['price'],
                            time_in_force=replace_order_message['time_in_force'],
                            firm=original_enter_message['firm'],
                            display=replace_order_message['display'],
                            order_reference_number=next(self.order_ref_numbers), 
                            capacity=b'*',
                            intermarket_sweep_eligibility = replace_order_message['intermarket_sweep_eligibility'],
                            minimum_quantity = replace_order_message['minimum_quantity'],
                            cross_type=b'*',
                            order_state=b'L' if entered_order is not None else b'D',
                            previous_order_token=replace_order_message['existing_order_token'],
                            bbo_weight_indicator=b'*',
                            midpoint_peg=original_enter_message['midpoint_peg']
                            )
                    r.meta = replace_order_message.meta
                    self.outgoing_messages.append(r)
                    self.order_store.add_to_order(r['replacement_order_token'], r)        
                    cross_messages = [m for ((id, fulfilling_order_id), price, volume) in crossed_orders 
                                        for m in self.process_cross(id, 
                                                    fulfilling_order_id, 
                                                    price, 
                                                    volume, 
                                                    timestamp=timestamp)]
                    self.outgoing_messages.extend(cross_messages)

                    bbo_message = None
                    if new_bbo_post_enter:
                        bbo_message = self.best_quote_update(replace_order_message, 
                            new_bbo_post_enter, timestamp)
                    elif new_bbo_post_cancel:
                        bbo_message = self.best_quote_update(replace_order_message, 
                            new_bbo_post_cancel, timestamp)
                    if bbo_message:
                        self.outgoing_broadcast_messages.append(bbo_message)

    async def send_outgoing_broadcast_messages(self):
        """Send Server OuchMessage to all connected clients"""
        while len(self.outgoing_broadcast_messages)>0:
            m = self.outgoing_broadcast_messages.popleft()
            if m.message_type == OuchServerMessages.Executed:
                # self.update_transaction_log(m)
                self.transaction_logger.update_log(transaction=m, timestamp=nanoseconds_since_midnight())
            await self.message_broadcast(m)
        
        self.book_logger.update_log(book=self.order_book, timestamp=nanoseconds_since_midnight())

    async def send_outgoing_messages(self):
        """Send Server OuchMessage directly to sender"""
        while len(self.outgoing_messages)>0:
            m = self.outgoing_messages.popleft()
            await self.order_reply(m)

    async def process_message(self, message):
        """process Client OuchMessage
        Args:
            message: a OuchClientMessages object representing a buy/sell or cancel order command
        Returns False, if unsupported message is received, otherwise, responds to corresponding
            sender.
        """

        # Perform operation associated with message type
        if message.message_type in self.handlers:
            timestamp = nanoseconds_since_midnight()
            self.handlers[message.message_type](message, timestamp)
            await self.send_outgoing_broadcast_messages()
        else:
            log.error("Unknown message type %s", message.message_type)
            return False

    async def modify_order(self, modify_order_message):
        raise NotImplementedError()

