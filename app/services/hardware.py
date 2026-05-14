"""
Hardware Service Module
Handles serial communication with POS peripherals (cash drawers, etc.)
"""
import logging
import platform

logger = logging.getLogger(__name__)

# Standard ESC/POS cash drawer kick command
# ESC p m t1 t2
# m = pin (0 = pin 2, 1 = pin 5)
# t1, t2 = pulse timing (on time, off time in 2ms units)
DRAWER_KICK_COMMAND = bytes([0x1B, 0x70, 0x00, 0x19, 0x19])  # ~50ms pulse


def list_serial_ports():
    """
    List available serial ports on the system.
    Returns list of port names (e.g., ['COM1', 'COM3'] on Windows, ['/dev/ttyUSB0'] on Linux)
    """
    ports = []
    try:
        import serial.tools.list_ports
        for port in serial.tools.list_ports.comports():
            ports.append({
                'device': port.device,
                'description': port.description,
                'hwid': port.hwid
            })
    except ImportError:
        logger.warning("pyserial not installed, cannot list ports")
    except Exception as e:
        logger.error(f"Error listing serial ports: {e}")
    
    return ports


def open_cash_drawer(port: str, timeout: float = 1.0) -> dict:
    """
    Open the cash drawer connected to the specified serial port.
    
    Args:
        port: Serial port name (e.g., 'COM3' on Windows, '/dev/ttyUSB0' on Linux)
        timeout: Connection timeout in seconds
        
    Returns:
        dict with 'success' (bool) and 'message' (str)
    """
    if not port:
        return {'success': False, 'message': 'No port specified'}
    
    try:
        import serial
    except ImportError:
        logger.error("pyserial not installed")
        return {'success': False, 'message': 'pyserial library not installed'}
    
    try:
        # Open serial connection
        # Most cash drawers work at 9600 baud, 8N1
        with serial.Serial(
            port=port,
            baudrate=9600,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout
        ) as ser:
            # Send the kick command
            bytes_written = ser.write(DRAWER_KICK_COMMAND)
            ser.flush()
            
            logger.info(f"Cash drawer kick sent to {port} ({bytes_written} bytes)")
            return {'success': True, 'message': f'Drawer opened on {port}'}
            
    except serial.SerialException as e:
        error_msg = str(e)
        logger.error(f"Serial error opening drawer on {port}: {error_msg}")
        return {'success': False, 'message': f'Serial error: {error_msg}'}
    except Exception as e:
        logger.error(f"Unexpected error opening drawer: {e}")
        return {'success': False, 'message': f'Error: {str(e)}'}


def test_drawer_connection(port: str) -> dict:
    """
    Test if cash drawer port is accessible.
    Opens the drawer as a test.
    """
    return open_cash_drawer(port)
