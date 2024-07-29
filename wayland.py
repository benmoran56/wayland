from __future__ import annotations

import os
import ctypes
import socket
import logging as _logging
import itertools as _itertools

from array import array as _array
from types import FunctionType
# from inspect import signature, Parameter

from xml.etree import ElementTree
from xml.etree.ElementTree import Element

__version__ = 0.3

logger = _logging.getLogger('wayland')
logger.addHandler(_logging.StreamHandler())


##################################
#    Data types and structures
##################################

class Array(ctypes.Structure):
    _pack_ = True
    _fields_ = [('length', ctypes.c_uint32),    # size 4
                ('value', ctypes.c_char * 28)]  # size 28

    def __init__(self, text: str):
        assert len(text) <= 28
        super().__init__(len(text), text.encode())

    def __repr__(self):
        return f"{self.__class__.__name__}(len={self.length}, text='{self.value.decode()}')"


class String(ctypes.Structure):
    _fields_ = [('length', ctypes.c_uint32),    # size 4
                ('value', ctypes.c_char * 27),  # size 27
                ('null', ctypes.c_byte)]        # size 1

    def __init__(self, text: str):
        assert len(text) <= 27
        super().__init__(len(text) + 1, text.encode())

    def __repr__(self):
        return f"{self.__class__.__name__}(len={self.length}, text='{self.value.decode()}')"


class Fixed(ctypes.Structure):
    _fields_ = [('_value', ctypes.c_uint32)]    # size 32

    def __init__(self, value):
        v = (int(value) << 8) + int((value % 1.0) * 256)
        super().__init__(v)

    def __int__(self):
        return int((self._value >> 8) + (self._value & 0xff) / 256.0)

    def __float__(self):
        return (self._value >> 8) + (self._value & 0xff) / 256.0

    def __repr__(self):
        return f"{self.__class__.__name__}({float(self)})"


class Header(ctypes.Structure):
    _pack_ = True
    _fields_ = [('id', ctypes.c_uint32),        # size 4
                ('opcode', ctypes.c_uint16),    # size 2
                ('size', ctypes.c_uint16)]      # size 2

    def __add__(self, other: bytes) -> bytes:
        return bytes(self) + other  # type: ignore

    def __repr__(self) -> str:
        return f"Header(id={self.id}, opcode={self.opcode}, size={self.size})"


_argument_types = {
    'int':      ctypes.c_int32,
    'uint':     ctypes.c_uint32,
    'fixed':    Fixed,
    'string':   String,
    'object':   ctypes.c_uint32,
    'new_id':   ctypes.c_uint32,
    'array':    Array,
    'fd':       ctypes.c_int32
}


# _python_types = {
#     'int':      int,
#     'uint':     int,
#     'fixed':    float,
#     'string':   str,
#     'object':   int,
#     'new_id':   int,
#     'array':    str,
#     'fd':       int
# }


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
        self.type_name = element.get('type')
        self.type = _argument_types[self.type_name]
        # self.python_type = _python_types[self.type_name]
        self.summary = element.get('summary')

    def __call__(self, value) -> bytes:
        # TODO: This tries to convert the argument value (a class) into bytes:
        print(self.name, value, self.type)
        return bytes(self.type(value))

    def __repr__(self) -> str:
        return f"{self.name}({self.type_name}={self.type.__name__})"


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

    def __repr__(self):
        return f"{self.name}(opcode={self.opcode}, args=[{', '.join((f'{a}' for a in self.arguments))}])"


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
        # Headers are 8 bytes
        size = ctypes.sizeof(Header) + len(bytestring)

        header = Header(id=self._interface.id, opcode=self.opcode, size=size)

        request = header + bytestring
        print(f"{self.name} sent message: {request}, {header}")

        self._client.send_request(self, request, *fds)

    def __repr__(self):
        return f"{self.name}(opcode={self.opcode}, args=[{', '.join((f'{a}' for a in self.arguments))}])"


# class Request:
#
#     def __init__(self, interface, element, opcode, arguments):
#         self._client = interface.protocol.client
#         self.opcode = opcode
#         self.arguments = arguments
#
#         self.name = element.get('name')
#         self.description = getattr(element.find('description'), 'text', "")
#         self.summary = element.find('description').get('summary') if self.description else ""
#
#     def _send(self, bytestring):
#         # TODO: Complete this method
#
#         # Headers are 8 bytes
#         size = 8 + len(bytestring)
#         # header = Header(id=???, opcode=self.opcode, size=size)
#         #
#         # request = header +
#         #
#         # self._client.send_request(self, request, *fds)
#
#     # def __call__(self, *args) -> bytes:
#     #     return b''.join(self.arguments[i](value) for i, value in enumerate(args))
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

        # TODO: do enums have opcodes?
        self._enums = [Enum(self, element) for element in self._element.findall('enum')]
        self._events = [Event(self, element, opc) for opc, element in enumerate(self._element.findall('event'))]

        for request in self._create_requests():
            setattr(self, request.name, request)

    def _create_requests(self):
        """Dynamically create `request` methods

        This method parses the xml element for `request` definitions,
        and dynamically creates callable Reqest classes from them. These
        Request instances are then assigned by name to the Interface,
        allowing them to be called like a normal Python methods.
        """
        for i, element in enumerate(self._element.findall('request')):
            request_name = element.get('name')

            # Arguments are callable objects that type cast and return bytes:
            arguments = [Argument(self, arg) for arg in element.findall('arg')]

            # Create a dynamic __call__ method with correct signature:
            signature = "self, " + ", ".join(arg.name for arg in arguments)
            call_string = " + ".join(f"arguments[{i}]({arg.name})" for i, arg in enumerate(arguments))
            source = f"def {request_name}({signature}):\n    self._send({call_string})"
            # Final source code should look something like:
            #
            #   def request_name(self, argument1, argument2):
            #       self._send(arguments[0](argument1) + arguments[1](argument2))

            # print(source, '\n')

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
        """A representaion of a Wayland Protocol

        Given a Wayland Protocol .xml file, all Interfaces classes will
        be dynamically generated at runtime.
        """

        self.client = client
        self._root = ElementTree.parse(filename).getroot()

        self.name = self._root.get('name')
        self.copyright = getattr(self._root.find('copyright'), 'text', "")

        self._interface_classes = {}

        # Iterate over all defined interfaces, and dynamically create
        # custom Interface classes using the _InterfaceBase class.
        # Opcodes are determined by enumeration order.
        for i, element in enumerate(self._root.findall('interface')):
            name = element.get('name')
            interface_class = type(name, (_InterfaceBase,), {'protocol': self, '_element': element, 'opcode': i})
            self._interface_classes[name] = interface_class

    def create_interface(self, name, oid):
        if name not in self._interface_classes:
            raise NameError(f"This Protocol does not define an interface named '{name}'.\n"
                            f"Valid interface names are : {list(self._interface_classes)}")

        return self._interface_classes[name](oid=oid)

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
    def __init__(self, *protocols: str):
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

        if not os.path.exists(path):
            raise FileNotFoundError(f"Wayland endpoint not found: {path}")

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0)
        self._sock.setblocking(False)
        self._sock.connect(path)

        self._protocols = dict()
        self.protocols = _ObjectSpace()

        for filename in protocols:
            if not os.path.exists(filename):
                raise FileNotFoundError(f"Protocol file was not found: {filename}")

            protocol = Protocol(client=self, filename=filename)
            self._protocols[protocol.name] = protocol
            # Temporary addition for easy access in the REPL:
            setattr(self.protocols, protocol.name, protocol)

        assert 'wayland' in self._protocols, "You must provide at minimum a wayland.xml protocol file."

        # Client side object ID generator:
        self._oid_generator = _itertools.cycle(range(1, 0xfeffffff))

        # A mapping of oids to interfaces:
        self._objects = {}

        # Create a global wl_display object:
        self.wl_display = self.create_interface(protocol='wayland', interface='wl_display')

    def _get_next_object_id(self) -> int:
        """Get the next available object ID

        """
        oid = next(self._oid_generator)

        while oid in self._objects:
            oid = next(self._oid_generator)

        return oid

    def create_interface(self, protocol: str, interface: str):
        protocol_class = self._protocols[protocol]

        object_id = self._get_next_object_id()
        interface_instance = protocol_class.create_interface(name=interface, oid=object_id)
        self._objects[object_id] = interface_instance

        return interface_instance

    def send_request(self, request, *fds):
        self._sock.sendmsg([request], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, _array("i", fds))])

    def fileno(self):
        """The fileno of the socket object

        This method exists to allow the class
        to be "selectable" (see the `select` module).
        """
        return self._sock.fileno()

    def select(self):
        # TODO: receive events from the server
        # (data, ancdata, msg_flags, address)
        data, ancdata, msg_flags, _ = self._sock.recvmsg(1024, socket.CMSG_SPACE(64))
        print("data, ancdata, msg_flags: ", data, ancdata, msg_flags)

    def __del__(self):
        if hasattr(self, '_sock'):
            self._sock.close()

    def __repr__(self):
        return f"{self.__class__.__name__}(socket='{self._sock.getpeername()}')"


# TODO: remove testing code:
if __name__ == '__main__':
    client = Client('/usr/share/wayland/wayland.xml')
