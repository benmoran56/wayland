from __future__ import annotations

import os
import abc
import socket
import itertools as _itertools
import threading as _threading

from struct import Struct
from types import SimpleNamespace as _NameSpace
from collections import deque as _deque
from collections import namedtuple as _namedtuple

from xml.etree import ElementTree
from xml.etree.ElementTree import Element, ParseError

from typing import Any

__version__ = 0.6

debug = False

def _debug_wayland(message: str) -> bool:
    if debug:
        print(f"wl:  {message}")
    return True


assert _debug_wayland(f"version: {__version__}")



class ObjectIDPool:

    def __init__(self, minimum: int, maximum: int):
        self._sequence = _itertools.cycle(range(minimum, maximum))
        self._recycle_pool = _deque()

    def __next__(self):
        if self._recycle_pool:
            return self._recycle_pool.popleft()

        oid = next(self._sequence)

        while oid in self._recycle_pool:
            oid = next(self._sequence)

        return oid

    def send(self, oid):
        self._recycle_pool.append(oid)


##################################
#          Exceptions
##################################

class WaylandException(BaseException):
    """Base Wayland Exception"""


class WaylandServerError(WaylandException):
    """A logical error from the Server."""


class WaylandSocketError(WaylandException, OSError):
    """Errors related to Socket IO."""


class WaylandProtocolError(WaylandException, FileNotFoundError):
    """A Protocol related error."""


##################################
#       Wayland data types
##################################

class WaylandType(abc.ABC):
    struct: Struct
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
    struct = Struct('i')
    length = struct.size

    def __init__(self, value: int):
        self.value = value

    def to_bytes(self) -> bytes:
        return self.struct.pack(self.value)

    @classmethod
    def from_bytes(cls, buffer: bytes) -> Int:
        return cls(cls.struct.unpack(buffer[:cls.length])[0])


class UInt(WaylandType):
    struct = Struct('I')
    length = struct.size

    def __init__(self, value: int):
        self.value = value

    def to_bytes(self) -> bytes:
        return self.struct.pack(self.value)

    @classmethod
    def from_bytes(cls, buffer: bytes) -> UInt:
        return cls(cls.struct.unpack(buffer[:cls.length])[0])


class Fixed(WaylandType):
    struct = Struct('I')
    length = struct.size

    def __init__(self, value: int):
        self.value = value

    def to_bytes(self) -> bytes:
        return self.struct.pack((int(self.value) << 8) + int((self.value % 1.0) * 256))

    @classmethod
    def from_bytes(cls, buffer: bytes) -> Fixed:
        unpacked = cls.struct.unpack(buffer[:cls.length])[0]
        return cls((unpacked >> 8) + (unpacked & 0xff) / 256.0)


class String(WaylandType):
    struct = Struct('I')

    def __init__(self, text: str):
        # length uint + text length + 4byte rounding
        self.length = 4 + len(text) + (-len(text) % 4)
        self.value = text

    def to_bytes(self) -> bytes:
        string_length = len(self.value) + 1
        padding = self.length - self.struct.size
        encoded = self.value.encode()
        return self.struct.pack(string_length) + encoded.ljust(padding, b'\x00')

    @classmethod
    def from_bytes(cls, buffer: bytes) -> String:
        length = cls.struct.unpack(buffer[:4])[0]   # 32-bit integer ('I')
        text = buffer[4:4+length-1].decode()        # strip padding byte
        return cls(text)


class Array(WaylandType):
    struct = Struct('I')

    def __init__(self, array: bytes):
        # length uint + text length + 4byte padding
        self.length = 4 + len(array) + (-len(array) % 4)
        self.value = array

    def to_bytes(self) -> bytes:
        length = len(self.value)
        padding_size = (4 - (length % 4))
        return self.struct.pack(length) + b'\x00' * padding_size

    @classmethod
    def from_bytes(cls, buffer: bytes) -> Array:
        length = cls.struct.unpack(buffer[:4])[0]      # 32-bit integer
        array = buffer[4:4+length]
        return cls(array)


class Header(WaylandType):
    struct = Struct('IHH')
    length = struct.size

    def __init__(self, oid, opcode, size):
        self.oid = oid
        self.opcode = opcode
        self.size = size
        self.value = self.struct.pack(oid, opcode, size)

    def to_bytes(self) -> bytes:
        return self.value

    @classmethod
    def from_bytes(cls, buffer) -> Header:
        return cls(*cls.struct.unpack(buffer))

    def __repr__(self):
        return f"{self.__class__.__name__}(oid={self.oid}, opcode={self.opcode}, size={self.size})"


class Object(UInt):

    def __init__(self, value: int):
        # Optional 'allow-null' (None) as 0:
        super().__init__(value if value else 0)


class NewID(UInt):

    def to_bytes(self) -> bytes:
        # Special case for wl_registry.bind()
        if isinstance(self.value, bytes):
            return self.value
        return super().to_bytes()


class FD(Int):
    pass


class _ObjectSpace:
    pass


##################################
#      Wayland abstractions
##################################

class Argument:

    _type_map = {
        'int': Int,
        'uint': UInt,
        'fixed': Fixed,
        'string': String,
        'object': Object,
        'new_id': NewID,
        'array': Array,
        'fd': FD,
    }

    def __init__(self, request, element):
        self._request = request
        self._element = element
        self.name = element.get('name')
        self.type_name = element.get('type')
        self.wl_type = self._type_map[self.type_name]
        self.summary = element.get('summary')
        self.allow_null = True if element.get('allow-null') else False

        self.interface = element.get('interface')
        self.returns_new_object = self.wl_type is NewID and self.interface

    def __call__(self, value) -> bytes:
        return self.wl_type(value).to_bytes()

    def from_bytes(self, buffer: bytes) -> WaylandType:
        return self.wl_type.from_bytes(buffer)

    def __repr__(self) -> str:
        return f"{self.name}: {self.wl_type.__name__}"


Entry = _namedtuple('Entry', 'name, value, summary')


class Enum:
    def __init__(self, interface, element):
        self._interface = interface
        self._element = element

        self.name = element.get('name')
        self.description = getattr(element.find('description'), 'text', "")
        self.summary = element.find('description').get('summary') if self.description else ""
        self.bitfield = element.get('bitfield', 'false')

        self.entries = [Entry(e.get('name'), e.get('value'), e.get('summary')) for e in self._element.findall('entry')]
        self.entries.sort(key=lambda e: e.value)

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

        # signature = tuple(f"{arg.name}={value}" for arg, value in zip(self.arguments, decoded_values))
        # print(f"Event({self.name}), arguments={signature}")
        self._interface.dispatch_event(self.name, *decoded_values)

    def __repr__(self):
        args = ', '.join((f'{a.name}={a.type_name}' for a in self.arguments))
        return f"{self.__class__.__name__}(name={self.name}, opcode={self.opcode}, args=({args}))"


class Request:

    def __init__(self, interface, element, opcode):
        self._protocol = interface.protocol
        self._client = interface.protocol.client
        self.parent_oid = interface.oid
        self.opcode = opcode

        self.name = element.get('name')
        self.description = getattr(element.find('description'), 'text', "")
        self.summary = element.find('description').get('summary') if self.description else ""

        self.arguments = [Argument(self, arg) for arg in element.findall('arg')]
        # TODO: attempt to update a custom signature/annotations for the __call__ method.

    def _send(self, bytestring, fds) -> None:
        """Attach a Header to the payload, and send."""
        size = Header.length + len(bytestring)
        header = Header(self.parent_oid, self.opcode, size)
        # final request and file descriptor payloads:
        request = header.to_bytes() + bytestring
        self._client.sendmsg(request, fds)

    def __call__(self, *args: Any) -> None | Interface:
        assert len(args) == len(self.arguments), f"expected {len(self.arguments)} arguments: {self.arguments}"
        interface = None
        bytestring = b''
        fds = b''

        for argument, value in zip(self.arguments, args):
            if argument.returns_new_object:
                interface = self._protocol.create_interface(argument.interface, value)
            if argument.wl_type is FD:
                fds += argument(value)
                continue
            bytestring += argument(value)

        self._send(bytestring, fds)
        return interface

    def __repr__(self):
        return f"{self.name}(opcode={self.opcode}, args=({', '.join((f'{a}' for a in self.arguments))}))"


class Interface:
    """Interface base class"""

    _element: Element
    protocol: Protocol
    opcode: int

    _handlers: dict

    def __init__(self, oid: int):
        self.oid = oid
        self.name = self._element.get('name')
        self.version = int(self._element.get('version'), 0)

        self.description = getattr(self._element.find('description'), 'text', "")
        self.summary = self._element.find('description').get('summary') if self.description else ""

        self.enums = [Enum(self, element) for element in self._element.findall('enum')]
        self.events = [Event(self, element, opc) for opc, element in enumerate(self._element.findall('event'))]
        self.event_types = [event.name for event in self.events]

        self.requests = [Request(self, elem, opcode) for opcode, elem in enumerate(self._element.findall('request'))]
        for request in self.requests:
            setattr(self, request.name, request)

        self._handlers = dict()
        for name in self.event_types:
            self._handlers[name] = []

    def dispatch_event(self, name, *args):
        for handler in self._handlers[name]:
            handler(*args)

    def set_handler(self, name, handler):
        self._handlers[name].append(handler)

    def remove_handler(self, name, handler):
        if handler in self._handlers[name]:
            self._handlers[name].remove(handler)

    def __repr__(self):
        return f"{self.__class__.__name__}(oid={self.oid}, opcode={self.opcode})"


class Protocol:
    def __init__(self, client: Client, filename: str) -> None:
        """A Wayland Protocol

        Given a Wayland Protocol .xml file, this class will dynamically
        introspect and define custom classes for all Interfaces defined
        within. This class should not be instantiated directly. It will
        automatically be created as part of a :py:class:`~wayland.Client`
        instance.

        Args:
            client: The parent Client to which this Protocol belongs.
            filename: The .xml file that contains the Protocol definition.
        """
        try:
            self._root = ElementTree.parse(filename).getroot()
        except (FileNotFoundError, ParseError)as e:
            raise WaylandProtocolError(e)

        self.client = client
        self.name = self._root.get('name')
        self.copyright = getattr(self._root.find('copyright'), 'text', "")
        assert _debug_wayland(f"> {self}: initializing...")

        self._interface_classes = {}

        # Iterate over all defined interfaces and dynamically create custom Interface
        # classes using the _InterfaceBase class. Opcodes are determined by enumeration order.
        for i, element in enumerate(self._root.findall('interface')):
            name = element.get('name')
            interface_class = type(name, (Interface,), {'protocol': self, '_element': element, 'opcode': i})
            self._interface_classes[name] = interface_class
            assert _debug_wayland(f"   * found interface: '{name}'")

    def bind_interface(self, name: str) -> Interface:
        """Create an Interface instance & bind to a global object."""
        # find global match:
        interface_global = self.client.global_objects[name]
        # make client-side object:
        interface_instance = self.create_interface(name)
        _string = String(name).to_bytes()
        _version = UInt(interface_global.version).to_bytes()
        _new_id = NewID(interface_instance.oid).to_bytes()
        combined_new_id = _string + _version + _new_id

        assert _debug_wayland(f"> {self}.bind: global {name}")
        self.client.wl_registry.bind(interface_global.name, combined_new_id)
        return interface_instance

    def create_interface(self, name: str, oid: int | None = None) -> Interface:
        """Create an Interface instance by name.

        Args:
            name: The Interface name.
            oid: If not provided, an oid will be generated by the Client.
        """
        if name not in self._interface_classes:
            raise WaylandProtocolError(f"The '{self.name}' Protocol does not define an interface named '{name}'")

        oid = oid or next(self.client.oid_pool)
        interface = self._interface_classes[name](oid=oid)
        assert _debug_wayland(f"> {self}.create_interface: {interface}")
        self.client.client_objects[oid] = interface
        return interface

    def delete_interface(self, oid: int) -> None:
        """Delete an Interface, by its oid.

        Args:
            oid: The object ID (oid) of the interface.
        """
        interface = self.client.client_objects.pop(oid)
        self.client.oid_pool.send(oid)      # to reuse later
        assert _debug_wayland(f"> {self}delete_interface: {interface}")

    @property
    def interface_names(self) -> list[str]:
        return list(self._interface_classes)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}('{self.name}')"


GlobalObject = _namedtuple('GlobalObject', 'name, interface, version')

##################################
#           User API
##################################

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

        Args:
            *protocols: The file path to one or more <protocol>.xml files.
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
            raise WaylandSocketError(f"Wayland endpoint not found: {path}")

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, 0)
        self._sock.setblocking(False)
        self._sock.connect(path)
        self._recv_buffer = b""

        assert _debug_wayland(f"connected to: {self._sock.getpeername()}")

        # Client side object ID generation:
        self.oid_pool = ObjectIDPool(minimum=1, maximum=0xfeffffff)

        self.client_objects = {}    # oid: Interface
        self.global_objects = {}    # interface_name: GlobalObject
        self.global_mapping = {}    # global_name: interface_name

        self.protocol_dict = {p.name: p for p in [Protocol(self, filename) for filename in protocols]}
        self.protocols = _NameSpace(self.protocol_dict)

        assert 'wayland' in self.protocol_dict, "You must provide at minimum a wayland.xml protocol file."

        # Create global display interface:
        self.wl_display = self.protocols.wayland.create_interface(name='wl_display')
        self.wl_display.set_handler('error', self._wl_display_error_handler)
        self.wl_display.set_handler('delete_id', self._wl_display_delete_id_handler)

        # Create global registry:
        self.wl_registry = self.wl_display.get_registry(next(self.oid_pool))
        self.wl_registry.set_handler('global', self._wl_registry_global)
        self.wl_registry.set_handler('global_remove', self._wl_registry_global_remove)

        self._sync_semaphore = _threading.Semaphore()

    def sync(self):
        """Helper shortcut for wl_display.sync calls.

        This method creates a throw-away ``wl_callback`` object, calls
        ``wl_display.sync``, and then blocks until the ``wl_callback.done``
        event is returned from the server.
        """
        wl_callback = self.wl_display.sync(next(self.oid_pool))
        wl_callback.set_handler('done', self._wl_display_sync_handler)
        self._sync_semaphore.acquire(True, 5)

    def fileno(self) -> int:
        """The fileno of the internal socket.

        This method allows the class to be "selectable" (see the ``select`` module).
        """
        return self._sock.fileno()

    def sendmsg(self, request: bytes, fds: bytes) -> None:
        self._sock.sendmsg([request], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, fds)])

    def receive(self) -> None:
        """Receive Events from the server."""
        _header_len = Header.length

        try:
            new_data, ancdata, msg_flags, _ = self._sock.recvmsg(4096, socket.CMSG_SPACE(64))
        except BlockingIOError:
            return
        except ConnectionError:
            raise WaylandSocketError(f"Socket is closed")

        if new_data == b"":
            raise WaylandSocketError(f"Socket is dead")

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
            interface = self.client_objects[header.oid]
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
        self.receive()

    def __del__(self) -> None:
        if hasattr(self, '_sock'):
            self._sock.close()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(socket='{self._sock.getpeername()}')"

    # Event handlers

    def _wl_display_delete_id_handler(self, oid):
        self.protocols.wayland.delete_interface(oid)

    def _wl_display_error_handler(self, oid: int, code: int, message: str):
        # TODO: map this to the right interface/enum/entry
        print("ERROR callback: ", oid, code, message)

    def _wl_display_sync_handler(self, _unused):
        self._sync_semaphore.release()

    def _wl_registry_global(self, name, interface, version):
        assert _debug_wayland(f"wl_registry global: {name}, {interface}, {version}")
        self.global_objects[interface] = GlobalObject(name, interface, version)
        self.global_mapping[name] = interface

    def _wl_registry_global_remove(self, name):
        assert _debug_wayland(f"wl_registry global_remove: {name}")
        interface_name = self.global_mapping[name]
        del self.global_mapping[name]
        del self.global_objects[interface_name]
        # TODO: anything to do for created objects?
