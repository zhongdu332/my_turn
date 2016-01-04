import asyncio
import json
import logging
import weakref

from . import message as msg

logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)
log = logging.getLogger()


class BaseTcpClient:
    READ_SIZE = 1024

    def __init__(self, host, port, *, loop=None):
        self.host = host
        self.port = port
        self.loop = loop
        self._is_closing = False
        self.r = None
        self.w = None
        self._read_task = None

    def start(self):
        asyncio.async(self.connect())

    @asyncio.coroutine
    def connect(self):
        self.r, self.w = yield from asyncio.open_connection(self.host, self.port, loop=self.loop)
        self.connected()
        self._read_task = asyncio.async(self._read_loop())

    def close(self):
        if self._is_closing:
            return
        self._is_closing = True

        if self.w:
            self.w.close()
        if self._read_task:
            self._read_task.cancel()

    @asyncio.coroutine
    def _read_loop(self):
        while True:
            buf = yield from self.r.read(self.READ_SIZE)
            if len(buf) == 0:
                break

            r = self._process(buf)
            if asyncio.iscoroutine(r):
                yield from r

        self.disconnected()
        self.close()

    def _process(self, buf):
        ''' 子类实现
        '''
        raise NotImplemented

    def connected(self):
        ''' 子类实现
        '''

    def disconnected(self):
        ''' 子类实现
        '''

    def send(self, buf):
        self.w.write(buf)

    @asyncio.coroutine
    def sendall(self, buf):
        self.w.write(buf)
        yield from self.w.drain()


class BaseDataClient(BaseTcpClient):
    MAX_SEQ = 2*16

    def __init__(self, connection_id, host, port, *, loop=None):
        super().__init__(host, port, loop = loop)
        self.connection_id = connection_id
        self._seq = 0
        self._is_binded = False
        self._buf = bytearray()

    def start(self):
        asyncio.async(self.connect(), loop=self.loop)

    def connected(self):
        log.info("data client connected")
        self.bind()

    def disconnected(self):
        pass

    @asyncio.coroutine
    def _process(self, buf):
        if self._is_binded:
            if len(self._buf)>0:
                r = self.process_binded_data(self._buf)
                if asyncio.iscoroutine(r):
                    yield from r
                del self._buf[:]
            else:
                r = self.process_binded_data(buf)
                if asyncio.iscoroutine(r):
                    yield from r
            return

        self._buf.extend(buf)
        buf = self._buf

        while True:
            if len(buf) < msg.Head.size():
                break
            if buf[0] != msg.Head.SYNC_BYTE:
                del buf[0]
                continue

            head = msg.Head.read(buf)
            if head.payload_len + msg.Head.size() < len(buf):
                break

            payload = buf[msg.Head.size():msg.Head.size()+head.payload_len]
            # process command
            try:
                cmd = msg.Command(head.command)
                cmd_str = 'process_%s' % cmd.name
                processor = getattr(self, cmd_str)
                if asyncio.iscoroutinefunction(processor):
                    yield from processor(head, payload)
                else:
                    processor(head, payload)
            except:
                pass

            del buf[:head.payload_len+msg.Head.size()]

    def bind(self):
        log.info("send bind request")
        head = msg.Head(command=msg.Command.ConnectionBind)
        req = {
            'connection_id' : self.connection_id
        }
        self.send_request(head, json.dumps(req).encode())

    def _send_msg(self, head, payload):
        head.payload_len = len(payload)
        buf = bytearray()
        buf.extend(head.write())
        buf.extend(payload)
        self.w.write(buf)

    def send_request(self, head, payload):
        head.sequence = self._gen_seq()
        self._send_msg(head, payload)

    def _gen_seq(self):
        self._seq = self._seq+1
        if self._seq >= self.MAX_SEQ:
            self._seq = 0
        return self._seq

    def process_ConnectionBindAck(self, head, payload):
        try:
            payload = json.loads(payload.decode())
            if payload['code'] == 200:
                self._is_binded = True

            print("bind ok")
            return
        except BaseException as e:
            print(e)

        print("failed")
        # raise RuntimeError("bind error")

    def process_binded_data(self, buf):
        ''' 处理bind后的数据，即relay的数据
        '''
        raise NotImplemented


class LocalClient(BaseTcpClient):
    ''' 连接本地端口的客户端
    '''
    def __init__(self, port, *, loop=None):
        super().__init__('127.0.0.1', port, loop=loop)
        self._data_client = None

    def set_data_client(self, data_client):
        self._data_client = data_client

    def disconnected(self):
        if self._data_client is None:
            return

        d = self._data_client()
        if d:
            d.close()

    def _process(self, buf):
        if self._data_client is None:
            return

        d = self._data_client()
        if d:
            d.send(buf)


class LocalDataClient(BaseDataClient):
    ''' 连接本地端口和数据通道的类
    '''
    LOCAL_PORT = 22

    @classmethod
    def set_local_port(cls, port):
        cls.LOCAL_PORT = port

    def __init__(self, connection_id, host, port, *, loop=None):
        super().__init__(connection_id, host, port, loop=loop)
        self._local_client = None

    @asyncio.coroutine
    def process_ConnectionBindAck(self, head, payload):
        super().process_ConnectionBindAck(head, payload)
        if self._is_binded:
            try:
                self._local_client = LocalClient(self.LOCAL_PORT, loop=self.loop)
                self._local_client.set_data_client(weakref.ref(self))
                yield from self._local_client.connect()
                print("local client connect")
            except BaseException as e:
                print(e)

    def process_binded_data(self, buf):
        if self._local_client:
            self._local_client.send(buf)

    def disconnected(self):
        super().disconnected()
        if self._local_client:
            self._local_client.close()


class MyTurnClient(BaseTcpClient):
    MAX_SEQ = 2**16
    data_client_class = LocalDataClient

    def set_cb(self, disconnected_cb):
        self._disconnected_cb = disconnected_cb

    def connected(self):
        self._buf = bytearray()
        self._seq = 0
        self._is_allocated = False

        # {connection_id: data_client}
        self._data_clients = {}

        self.allocate()

    def disconnected(self):
        print("turn client connected")
        if hasattr(self, "_disconnected_cb"):
            self._disconnected_cb(self)

    def _process(self, buf):
        self._buf.extend(buf)
        buf = self._buf

        while True:
            if len(buf) < msg.Head.size():
                break
            if buf[0] != msg.Head.SYNC_BYTE:
                del buf[0]
                continue

            head = msg.Head.read(buf)
            if head.payload_len + msg.Head.size() < len(buf):
                break

            payload = buf[msg.Head.size():msg.Head.size()+head.payload_len]
            # process command
            try:
                cmd = msg.Command(head.command)
                cmd_str = 'process_%s' % cmd.name
                getattr(self, cmd_str)(head, payload)
            except:
                pass

            del buf[:head.payload_len+msg.Head.size()]

    def allocate(self):
        head = msg.Head(command=msg.Command.Allocation)
        req = {
            'software': '0.0.1',
        }
        self._send_msg(head, json.dumps(req).encode())


    def process_AllocationAck(self, head, payload):
        payload = json.loads(payload.decode())
        if payload['code'] != 200:
            raise RuntimeError("allocation failed")

        relay_address = payload['relay_address']
        log.info("allocate ok: %s" % relay_address)
        self._is_allocated = True

    def process_ConnectionAttamp(self, head, payload):
        log.info("process ConnectionAttamp")
        payload = json.loads(payload.decode())
        log.info(payload)
        connection_id = payload.get('connection_id')
        data_address = payload.get('data_address')
        if connection_id is None or data_address is None:
            raise RuntimeError("connection attamp failed")

        try:
            v = data_address.split(':')
            host = v[0]
            port = int(v[1])
            print(host, port)
            dc = self.data_client_class(connection_id, host, port, loop=self.loop)
            dc.start()
        except Exception as e:
            print(e)

        self._data_clients[connection_id] = dc


    def _send_msg(self, head, payload):
        head.payload_len = len(payload)
        buf = bytearray()
        buf.extend(head.write())
        buf.extend(payload)
        self.w.write(buf)

    def send_request(self, head, payload):
        head.sequence = self._gen_seq()
        self._send_msg(head, payload)

    def _gen_seq(self):
        self._seq = self._seq+1
        if self._seq >= self.MAX_SEQ:
            self._seq = 0
        return self._seq


class ForeverMyTurnClient:
    def __init__(self, host, port, *, loop=None):
        self.host = host
        self.port = port
        self.loop = loop
        self._client = None

    def start(self):
        asyncio.async(self.run())

    @asyncio.coroutine
    def run(self):
        if self._client:
            return

        while True:
            self._client = MyTurnClient(args.host, args.port, loop=loop)
            self._client.set_cb(self.on_client_disconnected)
            try:
                yield from asyncio.wait_for(self._client.connect(), 10.0)
                break
            except:
                print("turn client connect failed")
                self._client.close()
                yield from asyncio.sleep(5.0)
                continue


    def on_client_disconnected(self, client):
        self._client = None
        self.loop.call_later(5.0, self.start)
        

if __name__=='__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("host", help="turn server host")
    parser.add_argument("port", help="turn server port", type=int)
    parser.add_argument("lport", help="local server port", type=int)
    args = parser.parse_args()
    log.info("host:%s port:%d local_port:%d" % (args.host, args.port, args.lport))

    LocalDataClient.set_local_port(args.lport)
    loop = asyncio.get_event_loop()
    ftc = ForeverMyTurnClient(args.host, args.port, loop=loop)
    ftc.start()
    loop.run_forever()