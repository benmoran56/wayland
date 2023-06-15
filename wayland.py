from __future__ import annotations

import os
import ctypes
import socket
import itertools

from xml.etree import ElementTree
from xml.etree.ElementTree import Element


__version__ = 0.1


# class Message(ctypes.Structure):
#     # TODO: test
#     _fields_ = [
#         ('oid', ctypes.c_uint32),
#         ('size', ctypes.c_uint16),
#         ('opcode', ctypes.c_uint16),
#     ]


class Argument:
    def __init__(self, parent, element):
        self._parent = parent
        self._element = element

        self.type = element.get('type')
        self.name = element.get('name')

        for (key, value) in element.items():
            setattr(self, key, value)

    def __repr__(self):
        return f"{self.name}={self.type})"


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
        return f"{self.name}({', '.join([a.name for a in self.arguments])})"


class Request:
    def __init__(self, interface, element):
        self._interface = interface
        self._element = element

        self.name = element.get('name')
        self.description = getattr(element.find('description'), 'text', "")
        self.summary = element.find('description').get('summary') if self.description else ""

        self.arguments = [Argument(self, element) for element in element.findall('arg')]

    def __repr__(self):
        argument_names = ', '.join([a.name for a in self.arguments])
        return f"{self.name}({argument_names})"


class _Interface:

    _protocol: Protocol
    _element: Element

    def __init__(self):

        self.name = self._element.get('name')
        self.version = int(self._element.get('version'), 0)

        self.description = getattr(self._element.find('description'), 'text', "")
        self.summary = self._element.find('description').get('summary') if self.description else ""

        self.enums = [Enum(self, element) for element in self._element.findall('enum')]
        self.events = [Event(self, element) for element in self._element.findall('event')]
        self.requests = [Request(self, element) for element in self._element.findall('request')]

    def __repr__(self):
        return f"{self.__class__.__name__}('{self.name}')"


class Protocol:
    def __init__(self, client: Client, filename: str):
        self._client = client
        self._root = ElementTree.parse(filename).getroot()

        self.name = self._root.get('name')
        self.copyright = getattr(self._root.find('copyright'), 'text', "")

        self._interface_classes = {}

        for element in self._root.findall('interface'):
            name = element.get('name')
            self._interface_classes[name] = type(name, (_Interface,), {'_protocol': self, '_element': element})

    def create_interface(self, name):
        if name not in self._interface_classes:
            raise NameError(f"This protocol does not define an interfaced named '{name}'.\n"
                            f"Valid interfaces: {list(self._interface_classes)}")

        return self._interface_classes[name]()

    def __repr__(self):
        return f"{self.__class__.__name__}('{self.name}')"


class Client:
    def __init__(self, *protocols: str):
        """Create a Wayland Client connection.

        The Client class establishes a connection to the Wayland
        domain socket.

        :param protocols: one or more protocol xml file paths.
        """
        endpoint = os.environ.get('WAYLAND_DISPLAY', 'wayland-0')

        if os.path.isabs(endpoint):
            path = endpoint
        else:
            path = os.path.join(os.environ.get('XDG_RUNTIME_DIR', ''), endpoint)

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

    def get_next_object_id(self) -> int:
        """Get the next available object ID."""
        oid = next(self._oid_generator)

        while oid in self._objects:
            oid = next(self._oid_generator)

        return oid

    def send(self, requests):
        # TODO
        pass

    def recv(self):

        # TODO
        pass

    def fileno(self):
        # Make class selectable:
        return self._sock.fileno()

    def __del__(self):
        if hasattr(self, '_sock'):
            self._sock.close()

    def __repr__(self):
        return f"{self.__class__.__name__}(socket='{self._sock.getpeername()}')"


#############################################################
# DEBUG

client = Client('/usr/share/wayland/wayland.xml')

