# ScratchABit - interactive disassembler
#
# Copyright (c) 2015 Paul Sokolovsky
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import sys
import binascii
import json
import logging as log

import idaapi
import idc

#
# ScratchABit API and code
#

START = 0
END = 1
PROPS = 2
BYTES = 3
FLAGS = 4

def str_area(area):
    if not area:
        return "Area(None)"
    return "Area(0x%x-0x%x, %s)" % (area[START], area[END], area[PROPS])

def area_props(area):
    return area[PROPS]


class InvalidAddrException(Exception):
    "Thrown when dereferencing address which doesn't exist in AddressSpace."
    pass


class AddressSpace:
    UNK = 0
    CODE = 0x01
    CODE_CONT = 0x02
    DATA = 0x04
    DATA_CONT = 0x08
    STR = 0x10  # Continuation is DATA_CONT

    def __init__(self):
        self.area_list = []
        # Map from area start address to area byte content
        self.area_bytes = {}
        # Map from area start address to each byte's flags
        self.area_byte_flags = {}
        # Map from referenced addresses to their properties
        self.addr_map = {}
        # Map from address to its label
        self.labels = {}
        # Map from address to its comment
        self.comments = {}
        # Map from address to its cross-reference records
        self.xrefs = {}
        # Map from code/data unit to properties of its args
        # at the very least, this should differentiate between literal
        # numeric values and addresses/offsets/pointers to other objects
        self.arg_props = {}
        # Cached last accessed area
        self.last_area = None

    def add_area(self, start, end, props):
        sz = end - start + 1
        bytes = bytearray(sz)
        flags = bytearray(sz)
        a = (start, end, props, bytes, flags)
        self.area_list.append(a)
        return a

    def area_no(self, area):
        return self.area_list.index(area)

    def addr2area(self, addr):
        if self.last_area:
            a = self.last_area
            if a[0] <= addr <= a[1]:
                return (addr - a[0], a)
        for a in self.area_list:
            if a[0] <= addr <= a[1]:
                self.last_area = a
                return (addr - a[0], a)
        return (None, None)

    def min_addr(self):
        return self.area_list[0][START]

    def load_content(self, file, addr,sz=None):
        off, area = self.addr2area(addr)
        to = off + sz if sz else None
        file.readinto(memoryview(area[BYTES])[off:to])

    def is_valid_addr(self, addr):
        off, area = self.addr2area(addr)
        return area is not None

    def get_byte(self, addr):
        off, area = self.addr2area(addr)
        if area is None:
            raise InvalidAddrException(addr)
        return area[BYTES][off]

    def get_bytes(self, addr, sz):
        off, area = self.addr2area(addr)
        if area is None:
            raise InvalidAddrException(addr)
        return area[BYTES][off:off + sz]

    def get_data(self, addr, sz):
        off, area = self.addr2area(addr)
        val = 0
        for i in range(sz):
            val = val | (area[BYTES][off + i] << 8 * i)
        return val

    def get_flags(self, addr):
        off, area = self.addr2area(addr)
        if area is None:
            raise InvalidAddrException(addr)
        return area[FLAGS][off]

    def get_unit_size(self, addr):
        off, area = self.addr2area(addr)
        flags = area[FLAGS]
        sz = 1
        if flags[off] == self.CODE:
            f = self.CODE_CONT
        elif flags[off] in (self.DATA, self.STR):
            f = self.DATA_CONT
        else:
            return 1
        off += 1
        while flags[off] == f:
            off += 1
            sz += 1
        return sz

    # Taking an offset inside unit, return offset to the beginning of unit
    @classmethod
    def adjust_offset_reverse(cls, off, area):
        flags = area[FLAGS]
        while off > 0:
            if flags[off] in (cls.CODE_CONT, cls.DATA_CONT):
                off -= 1
            else:
                break
        return off


    def set_flags(self, addr, sz, head_fl, rest_fl=0):
        off, area = self.addr2area(addr)
        flags = area[FLAGS]
        flags[off] = head_fl
        off += 1
        for i in range(sz - 1):
            flags[off + i] = rest_fl

    def make_undefined(self, addr, sz):
        self.set_flags(addr, sz, self.UNK, self.UNK)

    def make_code(self, addr, sz):
        off, area = self.addr2area(addr)
        area_byte_flags = area[FLAGS]
        area_byte_flags[off] |= self.CODE
        for i in range(sz - 1):
            area_byte_flags[off + 1 + i] |= self.CODE_CONT

    def make_data(self, addr, sz):
        off, area = self.addr2area(addr)
        area_byte_flags = area[FLAGS]
        area_byte_flags[off] |= self.DATA
        for i in range(sz - 1):
            area_byte_flags[off + 1 + i] |= self.DATA_CONT

    def get_default_label_prefix(self, ea):
        fl = self.get_flags(ea)
        if fl == self.CODE:
            prefix = "loc_"
        elif fl & self.DATA:
            prefix = "dat_"
        else:
            prefix = "unk_"
        return prefix

    def get_default_label(self, ea):
        prefix = self.get_default_label_prefix(ea)
        return "%s%08x" % (prefix, ea)

    def make_label(self, prefix, ea):
        if ea in self.labels:
            return
        if not prefix:
            prefix = self.get_default_label_prefix(ea)
        self.labels[ea] = "%s%08x" % (prefix, ea)

    # auto_label will change its prefix automatically based on
    # type of data it points.
    def make_auto_label(self, ea):
        if ea in self.labels:
            return
        self.labels[ea] = ea

    def get_label(self, ea):
        label = self.labels.get(ea)
        if isinstance(label, int):
            return "%s%08x" % (self.get_default_label_prefix(ea), label)
        return label

    def set_label(self, ea, label):
        self.labels[ea] = label

    def make_unique_label(self, ea, label):
        existing = self.get_label_set()
        cnt = 0
        while True:
            l = label
            if cnt > 0:
                l += "_%d" % cnt
            if l not in existing:
                self.labels[ea] = l
                return l
            cnt += 1

    def get_label_list(self):
        return sorted([x if isinstance(x, str) else self.get_default_label(x) for x in self.labels.values()])

    def get_label_set(self):
        return set(x if isinstance(x, str) else self.get_default_label(x) for x in self.labels.values())

    def resolve_label(self, label):
        for ea, l in self.labels.items():
            if l == label:
                return ea
            if isinstance(l, int) and self.get_default_label(l) == label:
                return ea
        return None

    def label_exists(self, label):
        return label in self.get_label_set()

    def get_comment(self, ea):
        return self.comments.get(ea)

    def set_comment(self, ea, comm):
        self.comments[ea] = comm

    def set_arg_prop(self, ea, arg_no, prop, prop_val):
        if ea not in self.arg_props:
            self.arg_props[ea] = {}
        arg_props = self.arg_props[ea]
        if arg_no not in arg_props:
            arg_props[arg_no] = {}
        props = arg_props[arg_no]
        props[prop] = prop_val

    def get_arg_prop(self, ea, arg_no, prop):
        return self.arg_props.get(ea, {}).get(arg_no, {}).get(prop)

    def add_xref(self, from_ea, to_ea, type):
        self.xrefs.setdefault(to_ea, {})[from_ea] = type

    def del_xref(self, from_ea, to_ea, type):
        if to_ea in self.xrefs:
            assert self.xrefs[to_ea][from_ea] == type
            del self.xrefs[to_ea][from_ea]

    def get_xrefs(self, ea):
        return self.xrefs.get(ea)

    # Persistance API
    def save_labels(self, stream):
        for addr in sorted(self.labels.keys()):
            l = self.labels[addr]
            if l == addr:
                # auto label
                stream.write("%08x\n" % addr)
            else:
                stream.write("%08x %s\n" % (addr, l))

    def load_labels(self, stream):
        for l in stream:
            vals = l.split()
            addr = int(vals[0], 16)
            if len(vals) > 1:
                label = vals[1]
            else:
                label = addr
            self.labels[addr] = label

    def save_comments(self, stream):
        for addr in sorted(self.comments.keys()):
            stream.write("%08x %s\n" % (addr, json.dumps(self.comments[addr])))

    def load_comments(self, stream):
        for l in stream:
            addr, comment = l.split(None, 1)
            addr = int(addr, 16)
            self.comments[addr] = json.loads(comment)

    def save_arg_props(self, stream):
        for addr in sorted(self.arg_props.keys()):
            stream.write("%08x %s\n" % (addr, json.dumps(self.arg_props[addr])))

    def load_arg_props(self, stream):
        for l in stream:
            addr, props = l.split(None, 1)
            addr = int(addr, 16)
            props = json.loads(props)
            # Stupud json can't have numeric keys
            props = {int(k): v for k, v in props.items()}
            self.arg_props[addr] = props

    def save_xrefs(self, stream):
        for addr in sorted(self.xrefs.keys()):
            stream.write("%08x\n" % addr)
            xrefs = self.xrefs[addr]
            for from_addr in sorted(xrefs.keys()):
                stream.write("%08x %s\n" % (from_addr, xrefs[from_addr]))
            stream.write("\n")

    def load_xrefs(self, stream):
        while True:
            l = stream.readline().rstrip()
            if not l:
                break
            addr = int(l, 16)
            while True:
                l = stream.readline().rstrip()
                if not l:
                    break
                from_addr, type = l.split()
                self.xrefs.setdefault(addr, {})[int(from_addr, 16)] = type

    def save_areas(self, stream):
        for a in self.area_list:
            stream.write("%08x %08x\n" % (a[START], a[END]))
            flags = a[FLAGS]
            i = 0
            while True:
                chunk = flags[i:i + 32]
                if not chunk:
                    break
                stream.write(str(binascii.hexlify(chunk), 'utf-8') + "\n")
                i += 32
            stream.write("\n")

    def load_areas(self, stream):
        for a in self.area_list:
            l = stream.readline()
            vals = [int(v, 16) for v in l.split()]
            assert a[START] == vals[0] and a[END] == vals[1]
            flags = a[FLAGS]
            i = 0
            while True:
                l = stream.readline().rstrip()
                if not l:
                    break
                l = binascii.unhexlify(l)
                flags[i:i + len(l)] = l
                i += len(l)


    # Hack for idaapi interfacing
    # TODO: should go to "Analysis" object
    @staticmethod
    def analisys_stack_push(ea):
        global analisys_stack
        analisys_stack.append(ea)


ADDRESS_SPACE = AddressSpace()
_processor = None
def set_processor(p):
    global _processor
    _processor = p
    idaapi.set_processor(p)


analisys_stack = []

def add_entrypoint(ea):
    analisys_stack.append(ea)

def init_cmd(ea):
    _processor.cmd.ea = ea
    _processor.cmd.size = 0
    _processor.cmd.disasm = None

def analyze(callback=lambda cnt:None):
    cnt = 0
    limit = 40000
    while analisys_stack and limit:
        ea = analisys_stack.pop()
        init_cmd(ea)
        try:
            insn_sz = _processor.ana()
        except InvalidAddrException:
            # Ran out of memory area, just continue
            # with the rest of paths
            continue
#        print("size: %d" % insn_sz, _processor.cmd)
        if insn_sz:
            if not _processor.emu():
                assert False
            ADDRESS_SPACE.make_code(ea, insn_sz)
            _processor.out()
#            print("%08x %s" % (_processor.cmd.ea, _processor.cmd.disasm))
#            print("---------")
            limit -= 1
            cnt += 1
            if cnt % 1000 == 0:
                callback(cnt)
#    if not analisys_stack:
#        print("Analisys finished")



class Model:

    def __init__(self, target_addr=0, target_subno=0):
        self._lines = []
        self._cnt = 0
        self._subcnt = 0
        self._last_addr = -1
        self._addr2line = {}
        self.AS = None
        self.target_addr = target_addr
        self.target_subno = target_subno
        self.target_addr_lineno_0 = -1
        self.target_addr_lineno = -1
        self.target_addr_lineno_real = -1

    def lines(self):
        return self._lines

    def add_line(self, addr, line):
        if addr != self._last_addr:
            self._last_addr = addr
            self._subcnt = 0
        if addr == self.target_addr:
            if self._subcnt == 0:
                # Contains first line related to the given addr
                self.target_addr_lineno_0 = self._cnt
            if self._subcnt == self.target_subno:
                # Contains line no. target_subno related to the given addr
                self.target_addr_lineno = self._cnt
            if not line.virtual:
                # Contains line where actual instr/data/unknown bytes are
                # rendered (vs labels/xrefs/etc.)
                self.target_addr_lineno_real = self._cnt
        self._lines.append(line)
        self._addr2line[(addr, self._subcnt)] = self._cnt
        line.subno = self._subcnt
        if not line.virtual:
            # Line of "real" disasm object
            self._addr2line[(addr, -1)] = self._cnt
        self._cnt += 1
        self._subcnt += 1

    def addr2line_no(self, addr):
        return self._addr2line.get((addr, -1))

    def undefine_unit(self, addr):
        sz = self.AS.get_unit_size(addr)
        self.AS.make_undefined(addr, sz)


def data_sz2mnem(sz):
    s = {1: "db", 2: "dw", 4: "dd"}[sz]
    return idaapi.fillstr(s, idaapi.DEFAULT_WIDTH)


class DisasmObj:

    # Size of "leader fields" in disasm window - address, raw bytes, etc.
    # May be set by MVC controller
    LEADER_SIZE = 9

    # Default indent for a line
    indent = "  "

    # Default operand positions list is empty and set on class level
    # to save memory. To be overriden on object level.
    arg_pos = ()

    # If False, this object corresponds to real bytes in input binary stream
    # If True, doesn't correspond to bytes in memory: labels, etc.
    virtual = True

    # Instance variable expected to be set on each instance:
    # ea =
    # size =
    # subno =  # relative no. of several lines corresponding to the same ea

    def render(self):
        # Render object as a string, set it as .cache, and return it
        pass

    def get_operand_addr(self):
        # Get "the most addressful" operand
        # This for example will be called when Enter is pressed
        # not on a specific instruction operand, so this should
        # return value of the operand which contains an address
        # (or the "most suitable" of them if there're few).
        return None

    def get_comment(self):
        comm = ADDRESS_SPACE.get_comment(self.ea) or ""
        if comm:
            comm = "  ; " + comm
        return comm

    def __len__(self):
        # Each object should return real character len as display on the screen.
        # Should be fast - called on each cursor movement.
        try:
            return self.LEADER_SIZE + len(self.indent) + len(self.cache)
        except AttributeError:
            return self.LEADER_SIZE + len(self.indent) + len(self.render())


class Instruction(idaapi.insn_t, DisasmObj):

    virtual = False

    def render(self):
        _processor.cmd = self
        _processor.out()
        s = self.disasm + self.get_comment()
        self.cache = s
        return s

    def get_operand_addr(self):
        # Assumes RISC design where only one operand can be address
        mem = imm = None
        for o in self._operands:
            if o.flags & idaapi.OF_SHOW:
                if o.type == idaapi.o_near:
                    # Jumps have priority
                    return o
                if o.type == idaapi.o_mem:
                    mem = o
                elif o.type == idaapi.o_imm:
                    imm = o
        if mem:
            return mem
        return imm

    def num_operands(self):
        for i, op in self._operands:
            if op.type == o_void:
                return i + 1
        return UA_MAXOP


class Data(DisasmObj):

    virtual = False

    def __init__(self, ea, sz, val):
        self.ea = ea
        self.size = sz
        self.val = val

    def render(self):
        if ADDRESS_SPACE.get_arg_prop(self.ea, 0, "type") == idaapi.o_mem:
            s = "%s%s" % (data_sz2mnem(self.size), ADDRESS_SPACE.get_label(self.val))
        else:
            s = "%s0x%x" % (data_sz2mnem(self.size), self.val)
        s += self.get_comment()
        self.cache = s
        return s

    def get_operand_addr(self):
        o = idaapi.op_t(0)
        o.value = self.val
        o.addr = self.val
        o.type = idaapi.o_imm
        return o


class String(DisasmObj):

    virtual = False

    def __init__(self, ea, sz, val):
        self.ea = ea
        self.size = sz
        self.val = val

    def render(self):
        s = "%s%s" % (data_sz2mnem(1), repr(self.val).replace("\\x00", "\\0"))
        s += self.get_comment()
        self.cache = s
        return s


class Unknown(DisasmObj):

    virtual = False
    size = 1

    def __init__(self, ea, val):
        self.ea = ea
        self.val = val

    def render(self):
        ch = ""
        if 0x20 <= self.val <= 0x7e:
            ch = " ; '%s'" % chr(self.val)
        s = "%s0x%02x%s" % (idaapi.fillstr("unk", idaapi.DEFAULT_WIDTH), self.val, ch)
        s += self.get_comment()
        self.cache = s
        return s


class Label(DisasmObj):

    indent = ""

    def __init__(self, ea):
        self.ea = ea

    def render(self):
        label = ADDRESS_SPACE.get_label(self.ea)
        s = "%s:" % label
        self.cache = s
        return s


class Xref(DisasmObj):

    indent = ""

    def __init__(self, ea, from_addr, type):
        self.ea = ea
        self.from_addr = from_addr
        self.type = type

    def render(self):
        s = "; xref: 0x%x %s" % (self.from_addr, self.type)
        self.cache = s
        return s

    def get_operand_addr(self):
        o = idaapi.op_t(0)
        o.addr = self.from_addr
        return o


class Literal(DisasmObj):

    def __init__(self, ea, str):
        self.ea = ea
        self.cache = str

    def render(self):
        return self.cache


def render():
    model = Model()
    render_partial(model, 0, 0, 1000000)
    return model

# How much bytes may a single disasm object (i.e. a line) occupy
MAX_UNIT_SIZE = 4

def render_partial_around(addr, subno, context_lines):
    log.debug("render_partial_around(%x, %d)", addr, subno)
    off, area = ADDRESS_SPACE.addr2area(addr)
    if area is None:
        return None
    back = context_lines * MAX_UNIT_SIZE
    off -= back
    if off < 0:
        area_no = ADDRESS_SPACE.area_no(area) - 1
        while area_no >= 0:
            area = ADDRESS_SPACE.area_list[area_no]
            sz = area[1] - area[0] + 1
            off += sz
            if off >= 0:
                break
            area_no -= 1
        if off < 0:
            # Reached beginning of address space, just set as such
            off = 0
    assert off >= 0
    log.debug("render_partial_around: %x, %s", off, str_area(area))
    off = ADDRESS_SPACE.adjust_offset_reverse(off, area)
    log.debug("render_partial_around adjusted: %x, %s", off, str_area(area))
    model = Model(addr, subno)
    render_partial(model, ADDRESS_SPACE.area_list.index(area), off, context_lines, addr)
    log.debug("render_partial_around model done, lines: %d", len(model.lines()))
    assert model.target_addr_lineno_0 >= 0
    if model.target_addr_lineno == -1:
        # If we couldn't find exact subno, use 0th subno of that addr
        # TODO: maybe should be last subno, because if we couldn't find
        # exact one, it was ~ last and removed, so current last is "closer"
        # to it.
        model.target_addr_lineno = model.target_addr_lineno_0
    return model


def render_partial(model, area_no, offset, num_lines, target_addr=-1):
    model.AS = ADDRESS_SPACE
    start = True
    #for a in ADDRESS_SPACE.area_list:
    while area_no < len(ADDRESS_SPACE.area_list):
        a = ADDRESS_SPACE.area_list[area_no]
        area_no += 1
        i = 0
        if start:
            i = offset
            start = False
        else:
            model.add_line(a[START], Literal(a[START], "; Start of 0x%x area" % a[START]))
        bytes = a[BYTES]
        flags = a[FLAGS]
        while i < len(flags):
            addr = a[START] + i
            # If we didn't yet reach target address, compensate for
            # the following decrement of num_lines. The logic is:
            # render all lines up to target_addr, and then num_lines past it.
            if target_addr >= 0 and addr < target_addr:
                num_lines += 1

            xrefs = ADDRESS_SPACE.get_xrefs(addr)
            if xrefs:
                for from_addr in sorted(xrefs.keys()):
                    model.add_line(addr, Xref(addr, from_addr, xrefs[from_addr]))

            label = ADDRESS_SPACE.get_label(addr)
            if label:
                model.add_line(addr, Label(addr))

            f = flags[i]
            if f == AddressSpace.UNK:
                out = Unknown(addr, bytes[i])
                i += 1
            elif f == AddressSpace.DATA:
                sz = 1
                j = i + 1
                while flags[j] == AddressSpace.DATA_CONT:
                    sz += 1
                    j += 1
                out = Data(addr, sz, ADDRESS_SPACE.get_data(addr, sz))
                i += sz
            elif f == AddressSpace.STR:
                str = chr(bytes[i])
                sz = 1
                j = i + 1
                while flags[j] == AddressSpace.DATA_CONT:
                    str += chr(bytes[j])
                    sz += 1
                    j += 1
                out = String(addr, sz, str)
                i += sz
            elif f == AddressSpace.CODE:
                out = Instruction(addr)
                _processor.cmd = out
                insn_sz = _processor.ana()
                _processor.out()
                i += insn_sz
            else:
                assert 0, "flags=%x" % f

            model.add_line(addr, out)
            #sys.stdout.write(out + "\n")
            num_lines -= 1
            if not num_lines:
                return

        model.add_line(a[END], Literal(a[END], "; End of 0x%x area" % a[START]))


def flag2char(f):
    if f == AddressSpace.UNK:
        return "."
    elif f == AddressSpace.CODE:
        return "C"
    elif f == AddressSpace.CODE_CONT:
        return "c"
    elif f == AddressSpace.DATA:
        return "D"
    elif f == AddressSpace.DATA_CONT:
        return "d"
    else:
        return "X"

def print_address_map():
    for a in ADDRESS_SPACE.area_list:
        for i in range(len(a[FLAGS])):
            if i % 128 == 0:
                sys.stdout.write("\n")
                sys.stdout.write("%08x " % (a[START] + i))
            sys.stdout.write(flag2char(a[FLAGS][i]))
        sys.stdout.write("\n")


idaapi.set_address_space(ADDRESS_SPACE)
idc.set_address_space(ADDRESS_SPACE)
