from __future__ import annotations

import os
import ctypes
import socket
import itertools

from xml.etree import ElementTree
from xml.etree.ElementTree import Element


__version__ = 0.1

##################################
#  Argument types and structures
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


argument_types = {
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
    _fields_ = [('oid', ctypes.c_uint32),           # size 4
                ('opcode', ctypes.c_uint16),        # size 2
                ('size', ctypes.c_uint16)]          # size 2

    def __repr__(self):
        return f"Header(id={self.oid}, opcode={self.opcode},  size={self.size})"


class Argument:
    def __init__(self, parent, element):
        self._parent = parent
        self._element = element

        self.type = element.get('type')
        self.name = element.get('name')

        for (key, value) in element.items():
            setattr(self, key, value)

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
    def __init__(self, interface, element):
        self._interface = interface
        self._element = element

        self.name = element.get('name')
        self.description = getattr(element.find('description'), 'text', "")
        self.summary = element.find('description').get('summary') if self.description else ""

        self.arguments = [Argument(self, element) for element in element.findall('arg')]

    def __repr__(self):
        return f"{self.name}({', '.join((f'{a.name}={a.type}' for a in self.arguments))})"


class Request:
    def __init__(self, interface, element):
        self._interface = interface
        self._element = element

        self.name = element.get('name')
        self.description = getattr(element.find('description'), 'text', "")
        self.summary = element.find('description').get('summary') if self.description else ""
        self.type = element.get('type')

        self.arguments = [Argument(self, element) for element in element.findall('arg')]

    def __repr__(self):
        return f"{self.name}({', '.join((f'{a.name}={a.type}' for a in self.arguments))})"


class _Interface:

    _protocol: Protocol
    _element: Element
    opcode: int

    def __init__(self, oid: int):
        """Interface base class"""
        self.id = oid
        self.name = self._element.get('name')
        self.version = int(self._element.get('version'), 0)

        self.description = getattr(self._element.find('description'), 'text', "")
        self.summary = self._element.find('description').get('summary') if self.description else ""

        self.enums = [Enum(self, element) for element in self._element.findall('enum')]
        self.events = [Event(self, element) for element in self._element.findall('event')]
        self.requests = [Request(self, element) for element in self._element.findall('request')]

    def __repr__(self):
        return f"{self.__class__.__name__}(opcode={self.opcode}, id={self.id})"


class Protocol:
    def __init__(self, client: Client, filename: str):
        self._client = client
        self._root = ElementTree.parse(filename).getroot()

        self.name = self._root.get('name')
        self.copyright = getattr(self._root.find('copyright'), 'text', "")

        self._interface_classes = {}

        # Iterate over all interfaces, and dynamically create
        # custom subclasses using  the _Interface base class.
        # Opcodes are determined by enumeration order.
        for opc, element in enumerate(self._root.findall('interface')):
            name = element.get('name')
            interface_class = type(name, (_Interface,), {'_protocol': self, '_element': element, 'opcode': opc})
            self._interface_classes[name] = interface_class

    def create_interface(self, name, oid):
        if name not in self._interface_classes:
            raise NameError(f"This protocol does not define an interfaced named"
                            f"'{name}'.\nValid interfaces: {list(self._interface_classes)}")

        return self._interface_classes[name](oid=oid)

    @property
    def interface_names(self):
        return list(self._interface_classes)

    def __repr__(self):
        return f"{self.__class__.__name__}('{self.name}')"


class Client:
    def __init__(self, *protocols: str):
        """Create a Wayland Client connection.

        The Client class establishes a connection to the Wayland
        domain socket. As per the Wayland specification, the
        `WAYLAND_DISPLAY` environmental variable is queried for
        the endpoint name. If this is an absolute path, it is
        used as-is. If not, the final socket path will be made
        by joining `XDG_RUNTIME_DIR` + `WAYLAND_DISPLAY`.

        The path to at least one Wayland Protocol definition file
        must be given. These are XML files, generally found under
        `/usr/share/wayland/`, which are used to generate the
        interfaces at runtime.

        :param protocols: one or more protocol xml file paths.
        """
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

        for filename in protocols:
            if not os.path.exists(filename):
                raise FileNotFoundError(f"Protocol file not found: {filename}")

            # TODO: Experimental direct access
            protocol = Protocol(client=self, filename=filename)
            setattr(self, f"protocol_{protocol.name}", protocol)

        # Client side object ID generator:
        self._oid_generator = itertools.cycle(range(1, 0xfeffffff))

        # A mapping of oids to interfaces:
        self._objects = {}

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

    # Event handlers



#############################################################
# DEBUG

client = Client('/usr/share/wayland/wayland.xml')
