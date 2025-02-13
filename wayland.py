from __future__ import annotations

import os
import abc
import socket
import struct
import itertools as _itertools

from array import array as _array
from types import FunctionType

from xml.etree import ElementTree
from xml.etree.ElementTree import Element


__version__ = 0.6


def _debug_wayland(message: str) -> bool:
    print(message)
    return True


assert _debug_wayland(f"version: {__version__}")


##################################
#    Data types and structures
##################################

class WaylandType(abc.ABC):
    length: int
    value: int | float | str | bytes

    @abc.abstractmethod
    def to_bytes(self) -> bytes:
        ...

    @classmethod
    @abc.abstractmethod
    def from_bytes(cls, buffer: bytes) -> WaylandType:
        ...

    def __repr__(self):
        return f"{self.__class__.__name__}(length={self.length}, value={self.value})"


class Int(WaylandType):
    length = struct.calcsize('i')

    def __init__(self, value: int):
        self.value = value

    def to_bytes(self) -> bytes:
        return struct.pack('i', self.value)

    @classmethod
    def from_bytes(cls, buffer: bytes) -> Int:
        return cls(struct.unpack('i', buffer[:cls.length])[0])


class UInt(WaylandType):
    length = struct.calcsize('I')

    def __init__(self, value: int):
        self.value = value

    def to_bytes(self) -> bytes:
        return struct.pack('I', self.value)

    @classmethod
    def from_bytes(cls, buffer: bytes) -> UInt:
        return cls(struct.unpack('I', buffer[:cls.length])[0])

class Fixed(WaylandType):
    length = struct.calcsize('I')

    def __init__(self, value: int):
        self.value = value

    def to_bytes(self) -> bytes:
        return struct.pack('I', (int(self.value) << 8) + int((self.value % 1.0) * 256))

    @classmethod
    def from_bytes(cls, buffer: bytes) -> Fixed:
        unpacked = struct.unpack('I', buffer[:cls.length])[0]
        return cls((unpacked >> 8) + (unpacked & 0xff) / 256.0)


class String(WaylandType):
    def __init__(self, text: str):
        # length uint + text length + 4byte padding
        self.length = 4 + len(text) + (-len(text) % 4)
        self.value = text

    def to_bytes(self) -> bytes:
        length = len(self.value) + 1
        padding = (4 - (length % 4))
        encoded = self.value.encode() + b'\x00'
        return struct.pack('I', length) + encoded.ljust(padding, b'\x00')

    @classmethod
    def from_bytes(cls, buffer: bytes) -> String:
        length = struct.unpack('I', buffer[:4])[0]      # 32-bit integer
        text = buffer[4:4+length-1].decode()
        return cls(text)


class Array(WaylandType):
    def __init__(self, array: bytes):
        # length uint + text length + 4byte padding
        self.length = 4 + len(array) + (-len(array) % 4)
        self.value = array

    def to_bytes(self) -> bytes:
        length = len(self.value)
        padding_size = (4 - (length % 4))
        return struct.pack('I', length) + b'\x00' * padding_size

    @classmethod
    def from_bytes(cls, buffer: bytes) -> Array:
        length = struct.unpack('I', buffer[:4])[0]      # 32-bit integer
        array = buffer[4:4+length]
        return cls(array)


class Header(WaylandType):
    length = struct.calcsize('IHH')

    def __init__(self, oid, opcode, size):
        self.oid = oid
        self.opcode = opcode
        self.size = size
        self.value = struct.pack('IHH', oid, opcode, size)

    def to_bytes(self) -> bytes:
        return self.value

    @classmethod
    def from_bytes(cls, buffer) -> Header:
        return cls(*struct.unpack('IHH', buffer))

    def __repr__(self):
        return f"{self.__class__.__name__}(oid={self.oid}, opcode={self.opcode}, size={self.size})"


class Object(UInt):
    pass


class NewID(UInt):
    pass


class FD(Int):
    pass


_argument_types = {
    'int':      Int,
    'uint':     UInt,
    'fixed':    Fixed,
    'string':   String,
    'object':   Object,
    'new_id':   NewID,
    'array':    Array,
    'fd':       FD,
}


class _ObjectSpace:
    pass


##################################
#      Wayland abstractions
##################################

class Argument:

    def __init__(self, parent, element):
        self._parent = parent
        self._element = element
        self.name = element.get('name')
        self.summary = element.get('summary')
        self.type_name = element.get('type')
        self.wl_type = _argument_types[self.type_name]

    def __call__(self, value) -> bytes:
        return self.wl_type(value).to_bytes()

    def from_bytes(self, buffer: bytes) -> WaylandType:
        return self.wl_type.from_bytes(buffer)

    def __repr__(self) -> str:
        return f"{self.name}({self.type_name}={self.wl_type.__name__})"


class Enum:
    def __init__(self, interface, element):
        self._interface = interface
        self._element = element

        self.name = element.get('name')
        self.description = getattr(element.find('description'), 'text', "")
        self.summary = element.find('description').get('summary') if self.description else ""

        self.bitfield = element.get('bitfield', 'false')

        self._entries = {}
        self._summaries = {}

        for entry in element.findall('entry'):
            name = entry.get('name')
            value = int(entry.get('value'), base=0)
            summary = entry.get('summary')
            self._entries[value] = name
            self._summaries[name] = summary

        # TODO: item access, including bitfield

    def __repr__(self):
        return f"{self.__class__.__name__}('{self.name}')"


class Event:
    def __init__(self, interface, element, opcode):
        self._interface = interface
        self._element = element
        self.opcode = opcode

        self.name = element.get('name')
        self.description = getattr(element.find('description'), 'text', "")
        self.summary = element.find('description').get('summary') if self.description else ""

        self.arguments = [Argument(self, element) for element in element.findall('arg')]

    def __call__(self, payload):
        decoded_values = []

        for arg in self.arguments:
            wl_type = arg.wl_type.from_bytes(payload)
            decoded_values.append(wl_type.value)
            # trim, and continue loop:
            payload = payload[wl_type.length:]

        # TODO: add event dispatcher to Client
        signature = tuple(f"{arg.name}={value}" for arg, value in zip(self.arguments, decoded_values))
        print(f"Event({self.name}), arguments={signature}")


    def __repr__(self):
        args = ', '.join((f'{a.name}={a.type_name}' for a in self.arguments))
        return f"{self.__class__.__name__}(name={self.name}, opcode={self.opcode}, args=({args}))"


class _RequestBase:
    """Request base class"""

    arguments: list

    def __init__(self, interface, element, opcode):
        self._interface = interface
        self._client = interface.protocol.client
        self.opcode = opcode

        self.name = element.get('name')
        self.description = getattr(element.find('description'), 'text', "")
        self.summary = element.find('description').get('summary') if self.description else ""

    def _send(self, bytestring, *fds):
        size = Header.length + len(bytestring)
        header = Header(oid=self._interface.id, opcode=self.opcode, size=size)
        request = header.to_bytes() + bytestring
        fds = b''.join(fd.to_bytes() for fd in fds)
        self._client.send_request(request, fds)

    def __repr__(self):
        return f"{self.name}(opcode={self.opcode}, args=[{', '.join((f'{a}' for a in self.arguments))}])"


# class Request:
#
#     def __init__(self, interface, element, opcode):
#         self._interface = interface
#         self._client = interface.protocol.client
#         self.opcode = opcode
#
#         self.name = element.get('name')
#         self.description = getattr(element.find('description'), 'text', "")
#         self.summary = element.find('description').get('summary') if self.description else ""
#
#         self.arguments = [Argument(self, arg) for arg in element.findall('arg')]
#
#     def _send(self, bytestring, *fds):
#         size = Header.length + len(bytestring)
#         header = Header(oid=self._interface.id, opcode=self.opcode, size=size)
#         request = header.to_bytes() + bytestring
#         fds = b''.join(fd.to_bytes() for fd in fds)
#         self._client.send_request(request, fds)
#
#     def __call__(self, *args) -> bytes:
#         return b''.join(self.arguments[i](value) for i, value in enumerate(args))
#
#     def __repr__(self):
#         return f"{self.name}(opcode={self.opcode}, args=[{', '.join((f'{a}' for a in self.arguments))}])"


class _InterfaceBase:
    """Interface base class"""

    _element: Element
    protocol: Protocol
    opcode: int

    def __init__(self, oid: int):
        self.id = oid
        self.version = int(self._element.get('version'), 0)

        self.description = getattr(self._element.find('description'), 'text', "")
        self.summary = self._element.find('description').get('summary') if self.description else ""

        self.enums = [Enum(self, element) for element in self._element.findall('enum')]
        self.events = [Event(self, element, opc) for opc, element in enumerate(self._element.findall('event'))]

        # TODO: figure out these requests
        # self._requests = [Request(self, element, opc) for opc, element in enumerate(self._element.findall('request'))]
        # TODO: or do like this?
        for request in self._create_requests():
            setattr(self, request.name, request)

    def _create_requests(self):
        """Dynamically create `request` methods

        This method parses the xml element for `request` definitions,
        and dynamically creates callable Request classes from them. These
        Request instances are then assigned by name to the Interface,
        allowing them to be called like a normal Python methods.
        """
        for i, element in enumerate(self._element.findall('request')):
            request_name = element.get('name')

            # Arguments are callable objects that type cast and return bytes:
            arguments = [Argument(self, arg) for arg in element.findall('arg')]

            # Create a dynamic __call__ method with correct signature:
            arg_names = [f"{arg.name}_{arg.type_name}" for arg in arguments]
            signature = "self, " + ", ".join(arg_names)
            call_string = " + ".join(f"arguments[{i}]({arg_name})" for i, arg_name in enumerate(arg_names))
            source = f"def {request_name}({signature}):\n    self._send({call_string})"
            ########## Final source code should look something like: ##############
            #
            #   def request_name(self, argument1, argument2):
            #       self._send(arguments[0](argument1) + arguments[1](argument2))
            #
            #######################################################################

            # Compile the source code into a function:
            compiled_code = compile(source=source, filename="<string>", mode="exec")
            method = FunctionType(compiled_code.co_consts[0], locals(), request_name)

            # Create a dynamic Request class which includes the custom __call__ method:
            request_class = type(request_name, (_RequestBase,), {'__call__': method, 'arguments': arguments})

            yield request_class(interface=self, element=element, opcode=i)

            # def _call(self, *args) -> bytes:
            #     return b''.join(self.arguments[_i](value) for _i, value in enumerate(args))
            #
            # # request = Request(interface=self, element=element, opcode=i, arguments=arguments)
            # request_class = type(request_name, (Request,), {'__call__': _call})
            #
            # sig = signature(_call)
            # parameters = [Parameter(name=arg.name, kind=Parameter.VAR_POSITIONAL) for arg in arguments]
            # _call.__signature__ = sig.replace(parameters=parameters)
            #
            # yield request_class(interface=self, element=element, opcode=i, arguments=arguments)

    def __repr__(self):
        return f"{self.__class__.__name__}(opcode={self.opcode}, id={self.id})"


class Protocol:
    def __init__(self, client: Client, filename: str):
        """A representation of a Wayland Protocol

        Given a Wayland Protocol .xml file, all Interfaces classes will
        be dynamically generated at runtime.
        """

        self.client = client
        self._root = ElementTree.parse(filename).getroot()

        self.name = self._root.get('name')
        self.copyright = getattr(self._root.find('copyright'), 'text', "")
        assert _debug_wayland(f" > Initializing Protocol: '{self.name}'")

        self._interface_classes = {}

        # Iterate over all defined interfaces, and dynamically create custom Interface
        # classes using the _InterfaceBase class. Opcodes are determined by enumeration order.
        for i, element in enumerate(self._root.findall('interface')):
            name = element.get('name')
            interface_class = type(name, (_InterfaceBase,), {'protocol': self, '_element': element, 'opcode': i})
            self._interface_classes[name] = interface_class
            assert _debug_wayland(f"   * found interface: '{name}'")

    def create_interface(self, name, oid):
        if name not in self._interface_classes:
            raise NameError(f"The '{self.name}' Protocol does not define an interface named '{name}'")

        assert _debug_wayland(f"{self}: creating Interface '{name}' with oid={oid}")
        interface = self._interface_classes[name](oid=oid)
        self.client.objects[oid] = interface
        return interface


    @property
    def interface_names(self):
        return list(self._interface_classes)

    def __repr__(self):
        return f"{self.__class__.__name__}('{self.name}')"


class Client:
    """Wayland Client

    The Client class establishes a connection to the Wayland domain socket.
    As per the Wayland specification, the `WAYLAND_DISPLAY` environmental
    variable is queried for the endpoint name. If this is an absolute path,
    it is used as-is. If not, the final socket path will be made by joining
    the `XDG_RUNTIME_DIR` + `WAYLAND_DISPLAY` environmental variables.

    To create an instance of this class, at least one Wayland Protocol file
    must be provided. Protocol files are XML, and are generally found under
    the `/usr/share/wayland/` directory. At a minimum, the base Wayland
    protocol file (`wayland.xml`) is required.

    When instantiated, the Client automatically creates the main Display
    (`wl_display`) interface, which is available as `Client.wl_display`.
    """
    def __init__(self, *protocols: str) -> None:
        """Create a Wayland Client connection.

        :param protocols: one or more protocol xml file paths.
        """
        assert protocols, ("At a minimum you must provide at least a wayland.xml "
                           "protocol file, commonly '/usr/share/wayland/wayland.xml'.")

        endpoint = os.environ.get('WAYLAND_DISPLAY', default='wayland-0')

        if os.path.isabs(endpoint):
            path = endpoint
        else:
            _runtime_dir = os.environ.get('XDG_RUNTIME_DIR', default='/run/user/1000')
            path = os.path.join(_runtime_dir, endpoint)

        assert _debug_wayland(f"endpoint: {path}")

        if not os.path.exists(path):
            raise FileNotFoundError(f"Wayland endpoint not found: {path}")

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0)
        self._sock.setblocking(False)
        self._sock.connect(path)
        self._recv_buffer = b""

        assert _debug_wayland(f"connected to: {self._sock.getpeername()}")

        # Client side object ID generator:
        self._oid_generator = _itertools.cycle(range(1, 0xfeffffff))

        # A mapping of oids to interfaces:
        self.objects = {}
        self.protocol_dict = dict()
        self.protocols = _ObjectSpace()

        for filename in protocols:
            if not os.path.exists(filename):
                raise FileNotFoundError(f"Protocol file was not found: {filename}")

            protocol = Protocol(client=self, filename=filename)
            self.protocol_dict[protocol.name] = protocol
            # Temporary addition for easy access in the REPL:
            setattr(self.protocols, protocol.name, protocol)

        assert 'wayland' in self.protocol_dict, "You must provide at minimum a wayland.xml protocol file."

        # Create a global wl_display object:
        next_id = self.get_next_object_id()
        self.wl_display = self.protocol_dict['wayland'].create_interface('wl_display', oid=next_id)

    def get_next_object_id(self) -> int:
        """Get the next available (unused) object ID"""
        oid = next(self._oid_generator)

        while oid in self.objects:
            oid = next(self._oid_generator)

        return oid

    def send_request(self, request: bytes, fds: bytes) -> None:
        self._sock.sendmsg([request], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)])

    def fileno(self) -> int:
        """The fileno of the socket object

        This method exists to allow the class
        to be "selectable" (see the ``select`` module).
        """
        return self._sock.fileno()

    def recv(self) -> None:
        _header_len = Header.length

        try:
            new_data, ancdata, msg_flags, _ = self._sock.recvmsg(4096, socket.CMSG_SPACE(64))
        except BlockingIOError:
            return

        # Include any leftover partial data:
        data = self._recv_buffer + new_data

        # Parse the events in chunks:
        while len(data) > _header_len:

            # The first part of the data is the header:
            header = Header.from_bytes(data[:_header_len])

            # Do we have enough data for the full message?
            if len(data) < header.size:
                break

            # - find the matching object (interface) from the header.oid
            # - find the matching event by its header.opcode
            # - pass the raw payload into the event, which will decode it
            interface = self.objects[header.oid]
            event = interface.events[header.opcode]
            event(data[_header_len:header.size])

            # trim, and continue loop
            data = data[header.size:]

        # Keep leftover for next time:
        self._recv_buffer = data

        for cmsg_level, cmsg_type, cmsg_data in ancdata:
            print("Unhandled ancillary data")
            # TODO: handle file descriptors and stuff

    def poll(self) -> None:
        self.recv()

    def __del__(self) -> None:
        if hasattr(self, '_sock'):
            self._sock.close()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(socket='{self._sock.getpeername()}')"
