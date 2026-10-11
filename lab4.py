#!/usr/bin/env python3
import yaml
import json
import requests
import asyncio
import aiohttp
import uuid

CONTROLLER_IP = "192.168.0.10"
CONTROLLER_PORT = "8080"
BASE_URL = f"http://{CONTROLLER_IP}:{CONTROLLER_PORT}"

class Student:
    def __init__(self, name, mac, code=None):
        self.name = name
        self.mac = mac.lower()
        self.code = code if code else name.replace(" ", "_").lower()

    def __repr__(self):
        return f"Student(name='{self.name}', mac='{self.mac}', code='{self.code}')"

class Service:
    def __init__(self, name, protocol, port):
        self.name = name.lower()
        self.protocol = protocol.lower()
        self.port = str(port)

    def __repr__(self):
        return f"Service(name='{self.name}', protocol='{self.protocol}', port='{self.port}')"

class Server:
    def __init__(self, name, ip, services, mac=None):
        self.name = name
        self.ip = ip
        self.mac = mac.lower() if mac else None
        self.services = [
            Service(s["name"], s["protocol"], s["port"]) if isinstance(s, dict) else s
            for s in services
        ]

    def get_service(self, service_name):
        for s in self.services:
            if s.name == service_name.lower():
                return s
        return None

class Course:
    def __init__(self, name, state, students, servers, code=None):
        self.name = name
        self.state = state.upper()
        self.students = students
        self.servers = servers
        self.code = code if code else ("HGW101" if "Potions" in name else ("HGW102" if "Transfiguration" in name else "HGW103"))

class Connection:
    def __init__(self, handler, student_mac, server_ip, service_name, flows):
        self.handler = handler
        self.student_mac = student_mac
        self.server_ip = server_ip
        self.service_name = service_name
        self.flows = flows

class NetworkPolicyManager:
    def __init__(self, students, servers, courses):
        self.students = {s.name: s for s in students}
        self.servers = {srv.name: srv for srv in servers}
        self.courses = {c.code: c for c in courses}
        self.connections = {}

    def is_student_authorized(self, student_name, server_name, service_name):
        """Valida que el alumno esté activo en un curso que permita el servicio en ese servidor."""
        for course in self.courses.values():
            if course.state != "ACTIVE":
                continue
            if student_name in course.students:
                for srv in course.servers:
                    if srv["name"] == server_name and service_name.lower() in srv.get("allowed_services", []):
                        return True
        return False

def load_data(filename):
    with open(filename, "r") as file:
        data = yaml.safe_load(file)
    students = [ Student(x["name"], x["mac"]) for x in data.get("students", []) ]
    servers = [ Server(y["name"], y["ip"], y["services"], y.get("mac")) for y in data.get("servers", []) ]
    courses = [ Course(z["name"], z["state"], z["students"], z["servers"], z.get("code")) for z in data.get("courses", []) ]
    return students, servers, courses

def get_attachment_point(controller_ip, mac):
    """Consulta Floodlight para obtener el switchDPID y el puerto real conectado a la MAC dada."""
    if not mac:
        return None, None
    url = f'http://{controller_ip}:8080/wm/device/'
    try:
        response = requests.get(url, timeout=3)
        devices = response.json()
        for dev in devices:
            macs = [m.lower() for m in dev.get("mac", [])]
            if mac.lower() in macs:
                attachment_points = dev.get("attachmentPoint", [])
                if attachment_points:
                    ap = attachment_points[0]
                    return ap.get("switchDPID"), ap.get("port")
        return None, None
    except Exception as e:
        print(f"[WARN] No se pudo obtener attachment point para {mac}: {e}")
        return None, None

def get_mac_by_ip(controller_ip, ip):
    """Busca en el Device Manager de Floodlight la MAC correspondiente a una IP."""
    url = f'http://{controller_ip}:8080/wm/device/'
    try:
        response = requests.get(url, timeout=3)
        for dev in response.json():
            if ip in dev.get("ipv4", []):
                macs = dev.get("mac", [])
                if macs:
                    return macs[0]
        return None
    except Exception:
        return None

def get_route(controller_ip, src_mac, dst_mac):
    src_sw_dpid, src_sw_port = get_attachment_point(controller_ip, src_mac)
    dst_sw_dpid, dst_sw_port = get_attachment_point(controller_ip, dst_mac)
    if not src_sw_dpid or not dst_sw_dpid:
        return []
    endpoint = f'/wm/topology/route/{src_sw_dpid}/{src_sw_port}/{dst_sw_dpid}/{dst_sw_port}/json'
    url = f'http://{controller_ip}:8080{endpoint}'
    try:
        response = requests.get(url, timeout=3)
        return response.json()
    except Exception as e:
        print(f"[WARN] Error obteniendo ruta: {e}")
        return []

async def push_flow_async(session, flow_entry):
    url = f"{BASE_URL}/wm/staticflowpusher/json"
    try:
        async with session.post(url, json=flow_entry) as resp:
            return await resp.json()
    except Exception as e:
        print(f"[ERROR] Error al insertar flujo {flow_entry.get('name')}: {e}")
        return None

async def delete_flow_async(session, flow_name):
    url = f"{BASE_URL}/wm/staticflowpusher/json"
    payload = {"name": flow_name}
    try:
        async with session.delete(url, json=payload) as resp:
            return await resp.json()
    except Exception as e:
        print(f"[ERROR] Error al eliminar flujo {flow_name}: {e}")
        return None

async def build_route(npm, student, server, service):
    handler = f"CONN-{uuid.uuid4().hex[:6].upper()}"
    proto_num = "6" if service.protocol == "tcp" else ("17" if service.protocol == "udp" else "1")

    # 1. Obtener DPID y Puerto del alumno
    src_dpid, src_port = get_attachment_point(CONTROLLER_IP, student.mac)

    # 2. Obtener MAC del servidor (de la BD o descubierta por IP)
    server_mac = server.mac or get_mac_by_ip(CONTROLLER_IP, server.ip)
    dst_dpid, dst_port = get_attachment_point(CONTROLLER_IP, server_mac) if server_mac else (None, None)

    # Si no se localiza dinámicamente el switch, se recurre al DPID detectado
    target_dpid = src_dpid or "00:00:1a:74:90:39:89:4a"

    # Determinar acción de puerto (si se detectó el puerto exacto se usa, de lo contrario FLOOD)
    out_action_a2s = f"output={dst_port}" if dst_port and dst_port > 0 else "output=FLOOD"
    out_action_s2a = f"output={src_port}" if src_port and src_port > 0 else "output=FLOOD"

    flows = [
        # Tráfico IP de Alumno -> Servidor
        {
            "switch": target_dpid,
            "name": f"{handler}-A2S-L4",
            "cookie": "0",
            "priority": "30000",
            "eth_type": "0x0800",
            "eth_src": student.mac,
            "ipv4_dst": server.ip,
            "ip_proto": proto_num,
            "tp_dst": service.port,
            "active": "true",
            "actions": out_action_a2s
        },
        # Tráfico de Respuesta Servidor -> Alumno
        {
            "switch": target_dpid,
            "name": f"{handler}-S2A-L4",
            "cookie": "0",
            "priority": "30000",
            "eth_type": "0x0800",
            "ipv4_src": server.ip,
            "eth_dst": student.mac,
            "ip_proto": proto_num,
            "tp_src": service.port,
            "active": "true",
            "actions": out_action_s2a
        },
        # Tráfico ARP Alumno -> Servidor
        {
            "switch": target_dpid,
            "name": f"{handler}-ARP-A2S",
            "cookie": "0",
            "priority": "29000",
            "eth_type": "0x0806",
            "eth_src": student.mac,
            "active": "true",
            "actions": "output=FLOOD"
        },
        # Tráfico ARP Servidor -> Alumno
        {
            "switch": target_dpid,
            "name": f"{handler}-ARP-S2A",
            "cookie": "0",
            "priority": "29000",
            "eth_type": "0x0806",
            "eth_dst": student.mac,
            "active": "true",
            "actions": "output=FLOOD"
        }
    ]

    async with aiohttp.ClientSession() as session:
        tasks = [push_flow_async(session, f) for f in flows]
        await asyncio.gather(*tasks)

    flow_names = [f["name"] for f in flows]
    npm.connections[handler] = Connection(handler, student.mac, server.ip, service.name, flow_names)
    return handler

async def remove_connection(npm, handler):
    if handler not in npm.connections:
        return False
    conn = npm.connections[handler]
    async with aiohttp.ClientSession() as session:
        tasks = [delete_flow_async(session, flow_name) for flow_name in conn.flows]
        await asyncio.gather(*tasks)
    del npm.connections[handler]
    return True

def menu():
    print("\n##################################################")
    print("Network Policy Manager de la UPSM")
    print("##################################################")
    print("\nSeleccione una opción:\n")
    print("1) Importar")
    print("2) Exportar")
    print("3) Cursos")
    print("4) Alumnos")
    print("5) Servidores")
    print("6) Políticas")
    print("7) Conexiones")
    print("8) Salir")

async def main_async():
    students, servers, courses = load_data("database.yaml")
    npm = NetworkPolicyManager(students, servers, courses)

    while True:
        menu()
        opt = input(">>> ").strip()

        if opt == "1":
            fn = input("Archivo YAML a importar: ").strip()
            try:
                st, sv, cr = load_data(fn)
                npm = NetworkPolicyManager(st, sv, cr)
                print("[OK] Datos importados exitosamente.")
            except Exception as e:
                print(f"[ERROR] No se pudo cargar el archivo: {e}")

        elif opt == "3":
            print("\n--- CURSOS ---")
            print("a) Listar (*)")
            print("b) Mostrar detalles (*)")
            print("c) Gestionar alumnos (*)")
            sub = input(">>> ").strip().lower()

            if sub == "a":
                print("\n[Lista de Cursos]")
                for c in npm.courses.values():
                    print(f"Código: {c.code} | Nombre: {c.name} | Estado: {c.state}")
            elif sub == "b":
                code = input("Código del curso: ").strip()
                if code in npm.courses:
                    c = npm.courses[code]
                    print(f"\nCurso: {c.name} ({c.code}) - Estado: {c.state}")
                    print("Alumnos matriculados:")
                    for st_name in c.students:
                        st_obj = npm.students.get(st_name)
                        mac_str = st_obj.mac if st_obj else "Desconocida"
                        print(f" - {st_name} (MAC: {mac_str})")
                else:
                    print("[ERROR] Curso no encontrado.")
            elif sub == "c":
                code = input("Código del curso: ").strip()
                if code in npm.courses:
                    c = npm.courses[code]
                    act = input("¿Desea (1) Agregar o (2) Eliminar alumno?: ").strip()
                    name = input("Nombre del alumno: ").strip()
                    if act == "1":
                        if name not in c.students:
                            c.students.append(name)
                        print(f"[OK] Alumno {name} agregado a {c.code}.")
                    elif act == "2":
                        if name in c.students:
                            c.students.remove(name)
                        print(f"[OK] Alumno {name} eliminado de {c.code}.")
                else:
                    print("[ERROR] Curso no encontrado.")

        elif opt == "4":
            print("\n--- ALUMNOS ---")
            print("a) Crear (*)")
            print("b) Listar (*)")
            print("c) Mostrar detalles (*)")
            sub = input(">>> ").strip().lower()

            if sub == "a":
                name = input("Nombre: ").strip()
                mac = input("MAC: ").strip()
                new_st = Student(name, mac)
                npm.students[name] = new_st
                print(f"[OK] Alumno {name} creado con MAC {mac}.")
            elif sub == "b":
                print("\n[Lista de Alumnos]")
                for s in npm.students.values():
                    print(f"Nombre: {s.name} | MAC: {s.mac}")
            elif sub == "c":
                name = input("Nombre del alumno: ").strip()
                if name in npm.students:
                    s = npm.students[name]
                    print(f"\n[Detalles Alumno]\nNombre: {s.name}\nCódigo: {s.code}\nMAC: {s.mac}")
                else:
                    print("[ERROR] Alumno no encontrado.")

        elif opt == "5":
            print("\n--- SERVIDORES ---")
            print("a) Listar (*)")
            print("b) Mostrar servicios (*)")
            sub = input(">>> ").strip().lower()

            if sub == "a":
                print("\n[Servidores Existentes]")
                for srv in npm.servers.values():
                    print(f"Nombre: {srv.name} | IP: {srv.ip}")
            elif sub == "b":
                sname = input("Nombre del servidor (ej. Server 3): ").strip()
                if sname in npm.servers:
                    srv = npm.servers[sname]
                    print(f"\nServicios de {srv.name} ({srv.ip}):")
                    for s in srv.services:
                        print(f" - {s.name.upper()}: Protocolo={s.protocol.upper()}, Puerto={s.port}")
                else:
                    print("[ERROR] Servidor no encontrado.")

        elif opt == "6":
            print("\n--- POLÍTICAS DE ACCESO ---")
            srv_target = input("Nombre del servidor (ej. Server 3): ").strip()
            svc_target = input("Nombre del servicio (ej. ssh): ").strip().lower()

            print(f"\n[Consulta de Políticas: {srv_target} -> {svc_target.upper()}]")

            cursos_con_acceso = []
            for course in npm.courses.values():
                for srv in course.servers:
                    if srv["name"] == srv_target and svc_target in srv.get("allowed_services", []):
                        cursos_con_acceso.append(course)

            if not cursos_con_acceso:
                print("No existen cursos configurados para este servicio.")
            else:
                for c in cursos_con_acceso:
                    if c.state == "ACTIVE":
                        print(f" -> [{c.code}] {c.name} | Estado: {c.state} (ACCESO PERMITIDO)")
                    else:
                        print(f" -> [{c.code}] {c.name} | Estado: {c.state} (DENEGADO por estar INACTIVO)")

        elif opt == "7":
            print("\n--- CONEXIONES ---")
            print("a) Crear (*)")
            print("b) Listar (*)")
            print("c) Borrar (*)")
            sub = input(">>> ").strip().lower()

            if sub == "a":
                sname = input("Nombre del alumno: ").strip()
                srv_name = input("Nombre del servidor (ej. Server 3): ").strip()
                svc_name = input("Nombre del servicio (ej. ssh, web): ").strip().lower()

                student = npm.students.get(sname)
                server = npm.servers.get(srv_name)
                service = server.get_service(svc_name) if server else None

                if not student or not server or not service:
                    print("[ERROR] Datos del alumno, servidor o servicio inválidos.")
                    continue

                if npm.is_student_authorized(student.name, server.name, service.name):
                    handler = await build_route(npm, student, server, service)
                    print(f"[ÉXITO] Conexión establecida con Handler: {handler}")
                else:
                    print("[ACCESO DENEGADO] El alumno no está autorizado para acceder a este servicio.")

            elif sub == "b":
                print("\n[Conexiones Creadas]")
                if not npm.connections:
                    print("No hay conexiones activas.")
                for h, conn in npm.connections.items():
                    print(f"Handler: {h} | MAC: {conn.student_mac} -> IP: {conn.server_ip} | Servicio: {conn.service_name}")

            elif sub == "c":
                h = input("Handler de la conexión a borrar: ").strip()
                ok = await remove_connection(npm, h)
                if ok:
                    print(f"[ÉXITO] Conexión {h} y sus reglas de flujo fueron eliminadas.")
                else:
                    print("[ERROR] Handler no encontrado.")

        elif opt == "8":
            print("Saliendo de la aplicación.")
            break

def main():
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
