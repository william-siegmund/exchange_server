"""
Client for simple Ouch Server(UNUSED BY CSE 115b/c team)
"""

import sys
import asyncio
import asyncio.streams
import configargparse
import logging as log
# import binascii
from random import randrange, randint
import itertools
from openai import OpenAI

from OuchServer.ouch_messages import OuchClientMessages, OuchServerMessages

p = configargparse.ArgParser()
p.add('--port', default=12345)
p.add('--host', default='127.0.0.1', help="Address of server")
p.add('--delay', default=0, type=float, help="Delay in seconds between sending messages")
p.add('--debug', action='store_true')
p.add('--time_in_force', default=99999, type=int)
options, args = p.parse_known_args()

def rounduprounddown(i, minlimit, maxlimit, roundeddown, roundedup):
    if i<minlimit:
        return roundeddown
    elif i>maxlimit:
        return roundedup
    else: 
        return i


class Client():
    def __init__(self):
        self.reader = None
        self.writer = None

    async def recv(self):
        try:
            header = (await self.reader.readexactly(1))
            print(f"header: {header}")
        except asyncio.IncompleteReadError:
            log.error('connection terminated without response')
            return None
        log.debug('Received Ouch header as binary: %r', header)
        log.debug('bytes: %r', list(header))
        message_type = OuchServerMessages.lookup_by_header_bytes(header)
        try:
            payload = (await self.reader.read(message_type.payload_size))
            print(f"payload {payload}")
        except asyncio.IncompleteReadError as err:
            log.error('Connection terminated mid-packet!')
            return None
        log.debug('Received Ouch payload as binary: %r', payload)
        log.debug('bytes: %r', list(payload))

        response_msg = message_type.from_bytes(payload, header=False)
        return response_msg

    async def recver(self):
        if self.reader is None:
            reader, writer = await asyncio.streams.open_connection(
            options.host, 
            options.port)
            self.reader = reader
            self.writer = writer
        index = 0
        while True:
            response = await self.recv()
            print(f"REsponse is THIS {response}")
            index += 1 
            while not self.reader.at_eof():
                response = await self.recv()
                index += 1
                # if index % 1000==0:
                print('received {} messages'.format(index))
            await asyncio.sleep(0.0000001)
            # if index % 1000==0:
            print('received {} messages'.format(index))
            #log.info('Received msg %s', response)
            #response = await recv()
            print('recv message: ', response)
            #log.debug("Received response Ouch message: %s", response)

    async def send(self, request):
        self.writer.write(bytes(request))
        await self.writer.drain()

    async def sender(self):
        if self.reader is None:
            reader, writer = await asyncio.streams.open_connection(
            options.host, 
            options.port)
            self.reader = reader
            self.writer = writer

        for index in itertools.count(): 
            order_token = int(input("Enter the order token number: "))
            request = OuchClientMessages.EnterOrder(
                order_token=f'{order_token:014d}'.encode('ascii'),
                buy_sell_indicator=b'B' if input("B or S: ") == 'B' else b'S',
                shares=1,#randrange(1,10**6-1),
                stock=b'AMAZGOOG',
                price=int(input("Enter price: ")),#rounduprounddown(randrange(1,100), 40, 60, 0, 2147483647 ),
                time_in_force=options.time_in_force,
                firm=b'OUCH',
                display=b'N',
                capacity=b'O',
                intermarket_sweep_eligibility=b'N',
                minimum_quantity=1,
                cross_type=b'N',
                customer_type=b' ',
                midpoint_peg=b' ',
                client_id=b'10')
                
            #print('send message: ', request)
            #log.info("Sending Ouch message: %s", request)
            await self.send(request)
            # data = await self.recv()
            # print(f"server repsonse {data}")
            # data = await self.reader.read(100)
            # print(f"Received {data}")
            reprequest = OuchClientMessages.ReplaceOrder(
                existing_order_token='{:014d}'.format(index).encode('ascii'),
                replacement_order_token='{:014d}'.format(900000000+index).encode('ascii'),
                shares=2*request['shares'],
                price=request['price'],
                time_in_force=options.time_in_force,
                display=b'N',
                intermarket_sweep_eligibility=b'N',
                minimum_quantity=1)
            #print('send message: ', request)
            #log.info("Sending Ouch message: %s", request)
            await self.send(reprequest)
            
            data = self.recv()
            data = await self.reader.read(250)
            print(f"Received: {data}, {type(data)}")

            reprequest = OuchClientMessages.ReplaceOrder(
                existing_order_token='{:014d}'.format(index).encode('ascii'),
                replacement_order_token='{:014d}'.format(900000000+index).encode('ascii'),
                shares=2*request['shares'],
                price=request['price'],
                time_in_force=options.time_in_force,
                display=b'N',
                intermarket_sweep_eligibility=b'N',
                minimum_quantity=1)
            #print('send message: ', request)
            #log.info("Sending Ouch message: %s", request)
            await self.send(reprequest)

            reprequestii = OuchClientMessages.ReplaceOrder(
                existing_order_token='{:014d}'.format(900000000+index).encode('ascii'),
                replacement_order_token='{:014d}'.format(910000000+index).encode('ascii'),
                shares=2*request['shares'],
                price=request['price'],
                time_in_force=options.time_in_force,
                display=b'N',
                intermarket_sweep_eligibility=b'N',
                minimum_quantity=1)
            #print('send message: ', request)
            #log.info("Sending Ouch message: %s", request)
            await self.send(reprequestii)


            cancelhalf = OuchClientMessages.CancelOrder(
                order_token='{:014d}'.format(910000000+index).encode('ascii'),
                shares=request['shares'])
            #print('send message: ', request)
            #log.info("Sending Ouch message: %s", request)
            await self.send(cancelhalf)            

            if index % 1000 == 0:
                print('sent {} messages'.format(index))   
            await asyncio.sleep(options.delay) 

    async def start(self):
        reader, writer = await asyncio.streams.open_connection(
            options.host, 
            options.port)
        self.reader = reader
        self.writer = writer
        await self.sender()
        writer.close()
        await asyncio.sleep(0.5)


def main():

    log.basicConfig(level=log.INFO if not options.debug else log.DEBUG)
    log.debug(options)

    client = Client()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # creates a client and connects to our server
    asyncio.ensure_future(client.recver(), loop=loop)
    asyncio.ensure_future(client.sender(), loop=loop)
    

    try:
        loop.run_forever()       
    finally:
        loop.close()

if __name__ == '__main__':
    # client = OpenAI()
    # userinput = input("enter prompt for openai: ")
    # completion = client.chat.completions.create(
    #     model="gpt-3.5-turbo",
    #     messages=[
    #         {"role": "system", "content": "Market Assistant"},
    #         {"role": "user", "content": userinput}
    #     ],
    # )

    # print(completion.choices[0].message)
    
    main()
    