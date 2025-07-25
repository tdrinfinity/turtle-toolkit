"""simulator.py - Singleton class for the simulator
Author: Tom Riley
Date: 2025-05-04
"""

from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Generator, Optional, Tuple, Union

from turtle_toolkit.assembler import Assembler
from turtle_toolkit.common.config import INSTRUCTION_WIDTH
from turtle_toolkit.common.data_types import (
    DataAddressBusValue,
    DataBusValue,
    InstructionAddressBusValue,
)
from turtle_toolkit.common.instruction_data import RegisterIndex
from turtle_toolkit.common.logger import logger
from turtle_toolkit.common.singleton_meta import SingletonMeta
from turtle_toolkit.modules.alu import ALU
from turtle_toolkit.modules.base_memory import BaseMemoryState
from turtle_toolkit.modules.base_module import BaseModuleState
from turtle_toolkit.modules.data_memory import DataMemory
from turtle_toolkit.modules.decoder import DecodedInstruction, DecodeUnit
from turtle_toolkit.modules.instruction_memory import (
    InstructionBinary,
    InstructionMemory,
)
from turtle_toolkit.modules.program_counter import ProgramCounter
from turtle_toolkit.modules.register_file import RegisterFile, RegisterFileState

ALU_NAME = "ALU"
DECODER_NAME = "Decoder"
INSTRUCTION_MEMORY_NAME = "InstructionMemory"
DATA_MEMORY_NAME = "DataMemory"
REGISTER_FILE_NAME = "RegisterFile"
PROGRAM_COUNTER_NAME = "ProgramCounter"

AddressTypes = Union[InstructionAddressBusValue, DataAddressBusValue]
DataTypes = Union[InstructionAddressBusValue, InstructionBinary]


class SimulationTimeout(Exception):
    """Exception raised when a simulation exceeds the watchdog timer limit."""

    def __init__(self, cycle_count: int):
        self.cycle_count = cycle_count
        super().__init__(f"Simulation timed out after {cycle_count} cycles")


@dataclass
class SimulatorState:
    """Class to hold the state of the simulator."""

    cycle_count: int = 0
    halted: bool = False
    stalled: bool = False
    modules: Dict[str, BaseModuleState] = field(default_factory=dict)


@dataclass
class SimulationResult:
    """Class to hold the result of the simulation."""

    cycle_count: int
    state: SimulatorState
    # Add other result variables as needed


class Simulator(metaclass=SingletonMeta):
    """Singleton class for the simulator."""

    def __init__(self):
        logger.debug("Initializing Simulator instance.")
        self.reset()
        logger.info("Simulator instance created.")

    def initialize_modules(self) -> None:
        self._state: SimulatorState
        self._alu: ALU = ALU(ALU_NAME)
        self._decode_unit: DecodeUnit = DecodeUnit(DECODER_NAME)
        self._instruction_memory: InstructionMemory = InstructionMemory(
            INSTRUCTION_MEMORY_NAME
        )
        self._data_memory: DataMemory = DataMemory(DATA_MEMORY_NAME)
        self._register_file: RegisterFile = RegisterFile(REGISTER_FILE_NAME)
        self._program_counter: ProgramCounter = ProgramCounter(PROGRAM_COUNTER_NAME)
        self._state.modules[self._instruction_memory.name] = (
            self._instruction_memory.get_state_ref()
        )
        self._state.modules[self._data_memory.name] = self._data_memory.get_state_ref()
        self._state.modules[self._register_file.name] = (
            self._register_file.get_state_ref()
        )
        self._state.modules[self._program_counter.name] = (
            self._program_counter.get_state_ref()
        )

    def _execute_cycle(self) -> SimulatorState:
        """Execute a single cycle of the simulation."""
        logger.debug(f"Executing cycle {self._state.cycle_count}.")

        # Fetch stage
        if not self._handle_fetch_stage():
            return self._state

        # Decode stage
        decoded_instruction = self._handle_decode_stage()
        if decoded_instruction is None:
            return self._state

        # Execute stage
        if not self._handle_execute_stage(decoded_instruction):
            return self._state

        # Memory stage
        if not self._handle_memory_stage(decoded_instruction):
            return self._state

        # Update program counter
        self._update_program_counter(decoded_instruction)

        return self._state

    def _handle_fetch_stage(self) -> bool:
        """Handle the fetch stage of the pipeline.
        Returns False if stalled."""
        instruction_address = self._program_counter.get_current_instruction_address()
        logger.debug(f"Fetching instruction from address {instruction_address}.")
        self._instruction_memory.request_fetch(instruction_address)

        if not self._instruction_memory.fetch_ready():
            self._state.stalled = True
            self._program_counter.set_stall(True)
            logger.debug("Instruction fetch not ready, skipping this cycle.")
            return False
        self._program_counter.set_stall(False)
        self._state.stalled = False

        logger.debug("Instruction fetch ready, proceeding.")
        return True

    def _handle_decode_stage(self) -> Optional[DecodedInstruction]:
        """Handle the decode stage of the pipeline.
        Returns None if the instruction should be skipped."""
        instruction = self._instruction_memory.get_fetch_result()
        logger.debug(f"Fetched instruction: {instruction}.")

        decoded_instruction = self._decode_unit.decode(instruction)

        if decoded_instruction.halt_instruction:
            logger.info("HALT instruction encountered, stopping simulation.")
            self._state.halted = True
            return None

        return decoded_instruction

    def _handle_execute_stage(self, decoded_instruction: DecodedInstruction) -> bool:
        """Handle the execute stage of the pipeline.
        Returns False if stalled."""
        # Get accumulator value
        logger.debug(f"Accumulator value: {self._register_file.get_acc_value()}.")

        # Handle ALU operations
        if decoded_instruction.alu_instruction:
            operand_b = self._get_alu_operand_b(decoded_instruction)
            if not self._execute_alu_operation(decoded_instruction, operand_b):
                return False

        # Handle register operations
        elif decoded_instruction.register_file_instruction:
            if not self._handle_register_operation(decoded_instruction):
                return False

        return True

    def _get_alu_operand_b(
        self, decoded_instruction: DecodedInstruction
    ) -> DataBusValue:
        """Get the second operand for ALU operations."""
        if decoded_instruction.alu_immediate_instruction:
            operand_b = decoded_instruction.immediate_data_value
            logger.debug(f"Using immediate value: {operand_b}.")
        else:
            operand_b = self._register_file.get_register_value(
                decoded_instruction.register_index
            )
            logger.debug(f"Using register value: {operand_b}.")
        return operand_b

    def _execute_alu_operation(
        self, decoded_instruction: DecodedInstruction, operand_b: DataBusValue
    ) -> bool:
        """Execute ALU operation and update state."""
        alu_outputs = self._alu.execute(
            self._register_file.get_acc_value(),
            operand_b,
            decoded_instruction.alu_function,
        )
        acc_next = alu_outputs.result
        self._register_file.set_next_acc_value(acc_next)
        self._register_file.set_next_status_register_value(
            alu_outputs.signed_overflow, alu_outputs.carry_flag
        )
        logger.debug(f"ALU result: {acc_next}.")
        return True

    def _handle_register_operation(
        self, decoded_instruction: DecodedInstruction
    ) -> bool:
        """Handle register file operations."""
        if decoded_instruction.register_file_set:
            acc_next = decoded_instruction.immediate_data_value
            self._register_file.set_next_acc_value(acc_next)
            logger.debug(f"Set accumulator to immediate value: {acc_next}.")
        elif decoded_instruction.register_file_get:
            acc_next = self._register_file.get_register_value(
                decoded_instruction.register_index
            )
            self._register_file.set_next_acc_value(acc_next)
            logger.debug(
                f"Get register {decoded_instruction.register_index} value: {acc_next}."
            )
        elif decoded_instruction.register_file_put:
            self._register_file.set_next_register_value(
                decoded_instruction.register_index, self._register_file.get_acc_value()
            )
            logger.debug(
                f"Set status register to {decoded_instruction.immediate_data_value}."
            )
        else:
            logger.fatal("Invalid register file operation. This should never happen.")
            raise RuntimeError("Invalid register file operation.")

        return True

    def _handle_memory_stage(self, decoded_instruction: DecodedInstruction) -> bool:
        """Handle the memory stage of the pipeline.
        Returns False if stalled."""
        if not decoded_instruction.memory_instruction:
            return True

        if decoded_instruction.memory_load:
            return self._handle_memory_load()
        elif decoded_instruction.memory_store:
            return self._handle_memory_store()
        else:
            logger.fatal("Invalid memory operation. This should never happen.")
            raise RuntimeError("Invalid memory operation.")

    def _handle_memory_load(self) -> bool:
        """Handle memory load operation."""
        self._data_memory.request_load(self._register_file.get_dmar_value())
        if not self._data_memory.load_ready():
            self._state.stalled = True
            self._program_counter.set_stall(True)
            logger.debug("Memory load not ready, skipping this cycle.")
            return False

        self._program_counter.set_stall(False)
        self._state.stalled = False

        acc_next = self._data_memory.get_load_result()
        self._register_file.set_next_acc_value(acc_next)
        logger.debug(f"Loaded value from memory: {acc_next}.")
        return True

    def _handle_memory_store(self) -> bool:
        """Handle memory store operation."""
        self._data_memory.request_store(
            self._register_file.get_dmar_value(), self._register_file.get_acc_value()
        )
        if not self._data_memory.store_complete():
            self._state.stalled = True
            self._program_counter.set_stall(True)
            logger.debug("Memory store not complete, skipping this cycle.")
            return False

        self._program_counter.set_stall(False)
        self._state.stalled = False

        logger.debug("Memory store complete.")
        return True

    def _update_program_counter(self, decoded_instruction: DecodedInstruction) -> None:
        """Update the program counter based on the instruction type."""
        if decoded_instruction.branch_instruction:
            self._program_counter.conditionally_branch(
                self._register_file.get_status_register_value(),
                decoded_instruction.immediate_address_value,
                decoded_instruction.branch_condition,
            )
        elif decoded_instruction.jump_instruction:
            self._handle_jump_instruction(decoded_instruction)
        else:
            self._program_counter.increment()

    def _handle_jump_instruction(self, decoded_instruction: DecodedInstruction) -> None:
        """Handle different types of jump instructions."""
        if decoded_instruction.immediate_jump:
            self._program_counter.jump_relative(
                decoded_instruction.immediate_address_value
            )
        elif decoded_instruction.relative_jump:
            self._program_counter.jump_relative(self._register_file.get_imar_value())
        else:
            self._program_counter.jump_absolute(self._register_file.get_imar_value())

    def _update_module_states(self) -> None:
        self._register_file.update_state()
        self._instruction_memory.update_state()
        self._data_memory.update_state()
        self._program_counter.update_state()

    def run(
        self, num_cycles: Optional[int] = None
    ) -> Generator[SimulatorState, None, SimulationResult]:
        logger.debug(f"Running simulator for {num_cycles} cycles.")
        cycles_run = 0
        while True:
            if num_cycles is not None and cycles_run >= num_cycles:
                logger.info("Reached the specified number of cycles.")
                break
            self._execute_cycle()
            cycles_run += 1
            self._state.cycle_count += 1
            logger.debug(
                f"Simulator tick: cycle count is now {self._state.cycle_count}."
            )
            if self._state.halted:
                logger.info(f"Simulation halted at cycle {self._state.cycle_count}.")
                break
            yield self._state
            self._update_module_states()
        logger.info(f"Simulation completed after {self._state.cycle_count} cycles.")
        return SimulationResult(self._state.cycle_count, self._state)

    def run_until_halt(self, max_cycles: Optional[int] = None) -> SimulationResult:
        """
        Run the simulation until a halt instruction is encountered or max_cycles is reached.

        Args:
            max_cycles (Optional[int]): Maximum number of cycles to run. If None, runs until halt.
                                        Acts as a watchdog timer to prevent infinite loops.

        Returns:
            SimulationResult: The result of the simulation.

        Raises:
            SimulationTimeout: If the simulation reaches max_cycles without halting.
        """
        gen = self.run(max_cycles)
        try:
            deque(gen, maxlen=0)  # Consume the generator to run the simulation
            next(gen)
        except StopIteration as e:
            logger.debug("Simulation completed.")
            result = getattr(e, "value", None)

            # If we reached max_cycles and simulation didn't halt naturally, raise timeout
            if (
                max_cycles is not None
                and self._state.cycle_count >= max_cycles
                and not self._state.halted
            ):
                raise SimulationTimeout(self._state.cycle_count)

            # If we don't have a result from the StopIteration, create one
            if result is None:
                result = SimulationResult(self._state.cycle_count, self._state)

            return result
        except Exception as e:
            formatted_state = self.format_simulator_state()
            logger.error(f"Simulation state:\n{formatted_state}")
            logger.error(f"Simulation failed: {e}")
            raise e
        raise RuntimeError("Simulation failed")

    def get_state(self) -> SimulatorState:
        """Get the current state of the simulator."""
        return self._state

    def _format_memory_contents(
        self, memory_dict: Dict[AddressTypes, DataTypes]
    ) -> str:
        """Format memory contents in a more readable way.

        Args:
            memory_dict: Dictionary of memory values
            address_type: Type of address (Instruction or Data)

        Returns:
            Formatted string representation of memory contents
        """
        if len(memory_dict) == 0:
            return "\tMemory is unwritten."

        result = []

        def _get_addr_unsigned_value(
            addr_value_pair: Tuple[AddressTypes, DataTypes],
        ) -> int:
            """Get the unsigned value of the address."""
            if hasattr(addr_value_pair[0], "unsigned_value"):
                return addr_value_pair[0].unsigned_value()
            else:
                raise ValueError(
                    f"Unsupported address type: {type(addr_value_pair[0])}"
                )

        for address, value in sorted(memory_dict.items(), key=_get_addr_unsigned_value):
            if isinstance(value, InstructionBinary):
                unsigned = int.from_bytes(value.data, byteorder="little")
                hex_width = len(value.data) * 2
                dec_width = len(str(2**INSTRUCTION_WIDTH - 1))
            else:
                unsigned = value.unsigned_value()
                hex_width = value._bus_width // 4
                dec_width = len(str(2**value._bus_width - 1))
            result.append(
                f"\t{address.unsigned_value():#0{((address._bus_width // 4)+2)}x}: {unsigned:<{dec_width}} ({unsigned:#0{hex_width+2}x})"
            )

        return "\n".join(result)

    def format_simulator_state(self) -> str:
        """Format the simulator state in a more readable way."""
        instr_mem_state: Optional[BaseModuleState] = self._state.modules.get(
            INSTRUCTION_MEMORY_NAME, None
        )
        data_mem_state: Optional[BaseModuleState] = self._state.modules.get(
            DATA_MEMORY_NAME, None
        )

        if instr_mem_state is None:
            raise RuntimeError(
                f"InstructionMemory module not found in state: {self._state.modules}"
            )
        if data_mem_state is None:
            raise RuntimeError(
                f"DataMemory module not found in state: {self._state.modules}"
            )
        if not isinstance(instr_mem_state, BaseMemoryState):
            raise RuntimeError(
                f"InstructionMemory state is not of type BaseMemoryState: {type(instr_mem_state)}"
            )
        if not isinstance(data_mem_state, BaseMemoryState):
            raise RuntimeError(
                f"DataMemory state is not of type BaseMemoryState: {type(data_mem_state)}"
            )

        instr_memory_dict = instr_mem_state.memory
        data_memory_dict = data_mem_state.memory

        reg_file_state = self._state.modules.get(REGISTER_FILE_NAME, None)

        if reg_file_state is None:
            raise RuntimeError(
                f"RegisterFile module not found in state: {self._state.modules}"
            )
        if not isinstance(reg_file_state, RegisterFileState):
            raise RuntimeError(
                f"RegisterFile state is not of type RegisterFileState: {type(reg_file_state)}"
            )

        result = [
            f"Simulator State (Cycle: {self._state.cycle_count}, Halted: {self._state.halted}, Stalled: {self._state.stalled})",
            "",
            "Instruction Memory:",
            self._format_memory_contents(instr_memory_dict),
        ]

        # Add data memory section
        result.extend(
            ["", "Data Memory:", self._format_memory_contents(data_memory_dict)]
        )

        # Add register file if available
        result.extend(["", "Register File:"])

        reg_name_max_len = max(len(member.name) for member in RegisterIndex)

        for reg, value in reg_file_state.registers.items():
            result.append(
                f"\t{reg.name:{reg_name_max_len}}: {value.unsigned_value():<4}({value.unsigned_value():#0{(value._bus_width // 4) + 2}x})"
            )

        return "\n".join(result)

    def reset(self) -> None:
        """Reset the simulator state."""
        logger.debug("Resetting simulator state.")
        self._state = SimulatorState()
        self.initialize_modules()
        logger.info("Simulator state reset.")

    def load_program(self, program: str) -> None:
        """Load a program into the instruction memory."""
        logger.debug("Loading program into instruction memory.")
        binary = Assembler.assemble(program)
        self._instruction_memory.side_load(binary)
        logger.info("Program loaded into instruction memory.")

    def load_binary(self, binary: bytes) -> None:
        """Load binary data into the instruction memory."""
        logger.debug("Loading binary data into instruction memory.")
        self._instruction_memory.side_load(binary)
        logger.info("Binary data loaded into instruction memory.")

    def load_binary_string_file(self, file_path: str) -> None:
        """Load binary data from a binary string format file (.binstr.txt).

        Args:
            file_path: Path to the binary string file

        The format expected is any text file containing binary digits (0 and 1).
        Comments (// to end of line) and all whitespace are ignored.
        Example formats that work:
            01000100 00000001  // SET 1
            00110100 00001010  // PUT DOFF
        Or:
            0100010000000001001101000000101000010100...
        Or:
            01000100
            00000001
            00110100
            00001010
        """
        logger.debug(f"Loading binary string file: {file_path}")

        try:
            with open(file_path, "r") as file:
                content = file.read()
        except IOError as e:
            logger.error(f"Error reading binary string file {file_path}: {e}")
            raise

        # Remove all comments (// to end of line)
        lines = content.split("\n")
        cleaned_lines = []
        for line in lines:
            # Remove everything after // (including //)
            if "//" in line:
                line = line[: line.index("//")]
            cleaned_lines.append(line)

        # Join all lines and remove all whitespace
        binary_text = "".join(cleaned_lines)
        binary_text = "".join(binary_text.split())  # Remove all whitespace

        # Validate that we only have binary digits
        if not all(c in "01" for c in binary_text):
            invalid_chars = set(c for c in binary_text if c not in "01")
            raise ValueError(
                f"Invalid characters in binary string: {invalid_chars}. Only '0' and '1' are allowed."
            )

        if len(binary_text) == 0:
            raise ValueError(f"No binary data found in file {file_path}")

        # Ensure we have complete instructions (multiple of 8 bits)
        if len(binary_text) % 8 != 0:
            padding_needed = 8 - (len(binary_text) % 8)
            logger.warning(
                f"Binary string length ({len(binary_text)}) is not a multiple of 8. Adding {padding_needed} zero bits for padding."
            )
            binary_text += "0" * padding_needed

        # Convert binary string to bytes
        binary_data = bytearray()
        for i in range(0, len(binary_text), 8):
            byte_str = binary_text[i : i + 8]
            byte_value = int(byte_str, 2)
            binary_data.append(byte_value)

        # Ensure we have complete instructions (even number of bytes)
        if len(binary_data) % 2 != 0:
            logger.warning(
                f"Binary data length ({len(binary_data)}) is odd. Adding padding byte."
            )
            binary_data.append(0)

        logger.info(
            f"Parsed {len(binary_data)} bytes ({len(binary_data)//2} instructions) from binary string file"
        )
        self.load_binary(bytes(binary_data))

    def get_data_memory_dump(self, dump_full_memory: bool = False) -> str:
        """Get the current data memory state as a binary string format.

        Args:
            dump_full_memory: If True, dumps entire memory space (0 to max address).
                             If False, dumps contiguous range from min to max written address.

        Returns:
            String containing the formatted memory dump.
        """
        logger.debug("Getting data memory state dump")

        data_mem_state = self._state.modules.get(DATA_MEMORY_NAME, None)
        if data_mem_state is None or not isinstance(data_mem_state, BaseMemoryState):
            raise RuntimeError("DataMemory state not found or invalid")

        # Create binary string format output
        lines = ["// Final data memory contents"]

        if len(data_mem_state.memory) == 0:
            if dump_full_memory:
                lines.append("// Memory is empty - showing full address space")
                # Get the data bus width to determine memory size
                # Assume 8-bit data width and typical address space
                max_address = 255  # 2^8 - 1 for 8-bit addressing
                for address in range(max_address + 1):
                    lines.append(f"{'0' * 8} // Address 0x{address:04x}")
            else:
                lines.append("// Memory is empty")
        else:
            # Get the range of addresses to dump
            written_addresses = [
                addr.unsigned_value() for addr in data_mem_state.memory.keys()
            ]
            min_addr = min(written_addresses)
            max_addr = max(written_addresses)

            if dump_full_memory:
                # Dump entire memory space from 0 to maximum possible address
                # For data memory, assume full address space based on address bus width
                first_addr = list(data_mem_state.memory.keys())[0]
                max_possible_addr = (1 << first_addr._bus_width) - 1
                dump_range = range(0, max_possible_addr + 1)
                lines.append(
                    f"// Dumping full memory space: 0x0000 to 0x{max_possible_addr:04x}"
                )
            else:
                # Dump contiguous range from min to max written address
                dump_range = range(min_addr, max_addr + 1)
                lines.append(
                    f"// Dumping contiguous range: 0x{min_addr:04x} to 0x{max_addr:04x}"
                )

            # Create a lookup for written memory locations
            memory_lookup = {
                addr.unsigned_value(): value
                for addr, value in data_mem_state.memory.items()
            }

            # Generate contiguous memory dump
            for address in dump_range:
                if address in memory_lookup:
                    # Memory location has been written
                    value = memory_lookup[address]
                    if hasattr(value, "unsigned_value"):
                        binary_str = format(
                            value.unsigned_value(), f"0{value._bus_width}b"
                        )
                        lines.append(f"{binary_str} // Address 0x{address:04x}")
                    else:
                        lines.append(
                            f"// Unknown value type at address 0x{address:04x}"
                        )
                else:
                    # Memory location is unwritten - fill with zeros
                    lines.append(f"{'0' * 8} // Address 0x{address:04x}")

        output_content = "\n".join(lines) + "\n"
        return output_content

    def get_register_file_dump(self) -> str:
        """Get the current register file state as a binary string format.

        Returns:
            String containing the formatted register dump.
        """
        logger.debug("Getting register file state dump")

        reg_file_state = self._state.modules.get(REGISTER_FILE_NAME, None)
        if reg_file_state is None or not isinstance(reg_file_state, RegisterFileState):
            raise RuntimeError("RegisterFile state not found or invalid")

        # Create binary string format output as a contiguous memory array
        lines = ["// Final register contents"]

        # Create a mapping from register index value to register enum
        register_by_index = {reg.value: reg for reg in RegisterIndex}

        # Find the maximum index to determine array size
        max_index = max(reg.value for reg in RegisterIndex)

        # Create contiguous array from index 0 to max_index
        for index in range(max_index + 1):
            if index in register_by_index:
                # Real register exists at this index
                reg_enum = register_by_index[index]
                if reg_enum in reg_file_state.registers:
                    value = reg_file_state.registers[reg_enum]
                    binary_str = format(value.unsigned_value(), f"0{value._bus_width}b")
                    lines.append(f"{binary_str} // {reg_enum.name}")
                else:
                    # Register enum exists but not in state (shouldn't happen)
                    lines.append(f"{'0' * 8} // {reg_enum.name} (not in state)")
            else:
                # Missing register index - fill with reserved placeholder
                lines.append(f"{'0' * 8} // RESERVED")

        output_content = "\n".join(lines) + "\n"
        return output_content
