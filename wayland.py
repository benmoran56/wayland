from __future__ import annotations

import os
import ctypes
import socket
import itertools

from types import FunctionType

from xml.etree import ElementTree
from xml.etree.ElementTree import Element


__version__ = 0.3


##################################
#    Data types and structures
##################################

class Array(ctypes.Structure):
    _pack_ = True
    _fields_ = [('length', ctypes.c_uint32),
                ('value', ctypes.c_char * 28)]
    def __init__(self, text: str):
        super().__init__(len(text), text.encode())
    def __repr__(self):
        return f"{self.__class__.__name__}(len={self.length}, text='{self.value.decode()}')"


class String(ctypes.Structure):
    _fields_ = [('length', ctypes.c_uint32),
                ('value', ctypes.c_char * 27),
                ('null', ctypes.c_byte)]
    def __init__(self, text: str):
        # Length including null byte
        super().__init__(len(text) + 1, text.encode())
    def __repr__(self):
        return f"{self.__class__.__name__}(len={self.length}, text='{self.value.decode()}')"


class Fixed(ctypes.Structure):
    _fields_ = [('_value', ctypes.c_uint)]
    def __init__(self, value):
        v = (int(value) << 8) + int((value % 1.0) * 256)
        super().__init__(v)
    def __int__(self):
        return int((self._value >> 8) + (self._value & 0xff) / 256.0)
    def __float__(self):
        return (self._value >> 8) + (self._value & 0xff) / 256.0
    def __repr__(self):
        return f"{self.__class__.__name__}({float(self)})"


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


class Header(ctypes.Structure):
    _pack_ = True
    _fields_ = [('id', ctypes.c_uint32),            # size 4
                ('opcode', ctypes.c_uint16),        # size 2
                ('size', ctypes.c_uint16)]          # size 2

    def __repr__(self):
        return f"Header(id={self.id}, opcode={self.opcode}, size={self.size})"


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
        self.interface = element.get('interface')
        self.summary = element.get('summary')

        # for (key, value) in element.items():
        #     # setattr(self, key, value)
        #     print(self.name, f"{key}={value}")

    def __call__(self, value):
        return bytes(self.type(value))

    def __repr__(self):
        return f"{self.name}=({self.type})"


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


class Request:
    def __init__(self, interface, element, opcode):
        self._interface = interface
        self._client = interface.protocol.client
        self._element = element
        self.opcode = opcode

        self.name = element.get('name')
        self.description = getattr(element.find('description'), 'text', "")
        self.summary = element.find('description').get('summary') if self.description else ""
        self.type = element.get('type')

        self.arguments = [Argument(self, element) for element in element.findall('arg')]

    def __call__(self, *args, **kwargs):
        print(f"Called {self.name} with {args}, {kwargs}")

    def __repr__(self):
        return f"{self.name}(opcode={self.opcode}, args=[{', '.join((f'{a}' for a in self.arguments))}])"


class _InterfaceBase:

    _element: Element
    protocol: Protocol
    opcode: int

    def __init__(self, oid: int):
        """Interface base class"""
        self.id = oid
        self.name = self._element.get('name')
        self.version = int(self._element.get('version'), 0)

        self.description = getattr(self._element.find('description'), 'text', "")
        self.summary = self._element.find('description').get('summary') if self.description else ""

        # TODO: do enums have opcodes?
        self._enums = [Enum(self, element) for element in self._element.findall('enum')]
        self._events = [Event(self, element, opc) for opc, element in enumerate(self._element.findall('event'))]
        self._requests = [Request(self, element, opc) for opc, element in enumerate(self._element.findall('request'))]

        print(f"Interface '{self.name}' defines the following requests:")
        for request in self._requests:
            setattr(self, request.name, request)
            print(f"  --> {request}")

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
    def __init__(self, *protocols: str):
        """Create a Wayland Client connection.

        The Client class establishes a connection to the Wayland domain socket.
        As per the Wayland specification, the `WAYLAND_DISPLAY` environmental
        variable is queried for the endpoint name. If this is an absolute path,
        it is used as-is. If not, the final socket path will be made by joining
        `XDG_RUNTIME_DIR` + `WAYLAND_DISPLAY`.

        The path to at least one Wayland Protocol definition file must be given.
        These are XML files, generally found under `/usr/share/wayland/`.
        Wayland Interfaces are generated from the definitions in these Protocol
        files.

        :param protocols: one or more protocol xml file paths.
        """
        assert protocols, ("At a minimum you must provide at least a wayland.xml "
                           "protocol file, commonly '/usr/share/wayland/wayland.xml'.")

        endpoint = os.environ.get('WAYLAND_DISPLAY', 'wayland-0')

        if os.path.isabs(endpoint):
            path = endpoint
        else:
            path = os.path.join(os.environ.get('XDG_RUNTIME_DIR', '/run/user/1000'), endpoint)

        if not os.path.exists(path):
            raise FileNotFoundError(f"Wayland endpoint not found: {path}")

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0)
        self._sock.setblocking(False)
        self._sock.connect(path)

        self.protocols = {}

        for filename in protocols:
            if not os.path.exists(filename):
                raise FileNotFoundError(f"Protocol file was not found: {filename}")

            protocol = Protocol(client=self, filename=filename)
            self.protocols[protocol.name] = protocol

            # TODO: Remove these expermentail direct instances:
            setattr(self, f"protocol_{protocol.name}", protocol)

        assert 'wayland' in self.protocols, "You must provide at minimum a wayland.xml protocol file."

        # Client side object ID generator:
        self._oid_generator = itertools.cycle(range(1, 0xfeffffff))

        # A mapping of oids to interfaces:
        self._objects = {}

        # Create a global display object:
        self.display = self.protocols['wayland'].create_interface('wl_display', self._get_next_object_id())

    def _get_next_object_id(self) -> int:
        """Get the next available object ID."""
        oid = next(self._oid_generator)

        while oid in self._objects:
            oid = next(self._oid_generator)

        return oid

    def send_request(self, request, *fds):
        # TODO: finish this
        import array
        self._sock.sendmsg([request], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))])

    def fileno(self):
        """The fileno of the socket object

        This method exists to allow the class
        to be "selectable" (see the `select` module).
        """
        return self._sock.fileno()

    def select(self):
        # TODO: receive events from the server
        # (data, ancdata, msg_flags, address)

        data, ancdata, msg_flags, _ = self._sock.recvmsg(1024, socket.CMSG_SPACE(16 * 4))

    def __del__(self):
        if hasattr(self, '_sock'):
            self._sock.close()

    def __repr__(self):
        return f"{self.__class__.__name__}(socket='{self._sock.getpeername()}')"
