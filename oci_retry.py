import oci
import os
import time
import datetime

INSTANCE_NAME = "arm-server"
UBUNTU_VERSION = "24.04"
ARM_OCPUS = 2
ARM_MEMORY_IN_GBS = 12
BOOT_VOLUME_SIZE_IN_GBS = 100
RETRY_INTERVAL = 90

config = oci.config.from_file()
COMPARTMENT_ID = os.environ.get("OCI_COMPARTMENT_ID", config["tenancy"])


def log(message):
    print(message, flush=True)


def load_ssh_public_key():
    path = config.get("ssh_public_key_file")

    if not path:
        raise SystemExit("ERROR: Falta la ruta de la clave SSH pública.")

    path = os.path.expanduser(path)

    with open(path, encoding="utf-8-sig") as file:
        key = file.read().strip()

    if not key or not key.startswith(("ssh-rsa ", "ssh-ed25519 ")):
        raise SystemExit("ERROR: OCI_SSH_PUBLIC_KEY no es una clave SSH pública válida.")

    return key


SSH_PUBLIC_KEY = load_ssh_public_key()


def get_availability_domains():
    identity = oci.identity.IdentityClient(config)
    response = identity.list_availability_domains(COMPARTMENT_ID)
    return [ad.name for ad in response.data]


def get_ubuntu_arm_image():
    compute = oci.core.ComputeClient(config)

    images = compute.list_images(
        COMPARTMENT_ID,
        operating_system="Canonical Ubuntu",
        operating_system_version=UBUNTU_VERSION,
        shape="VM.Standard.A1.Flex",
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data

    if not images:
        raise SystemExit(
            f"ERROR: No se encontró imagen Ubuntu {UBUNTU_VERSION} para ARM."
        )

    return images[0].id


def create_vcn_and_subnet():
    network = oci.core.VirtualNetworkClient(config)
    retry = oci.retry.DEFAULT_RETRY_STRATEGY

    vcns = network.list_vcns(
        COMPARTMENT_ID,
        display_name="retry-vcn",
        retry_strategy=retry,
    ).data

    if vcns:
        vcn = vcns[0]
        log(f"Usando VCN existente: {vcn.id}")
    else:
        vcn = network.create_vcn(
            oci.core.models.CreateVcnDetails(
                compartment_id=COMPARTMENT_ID,
                display_name="retry-vcn",
                cidr_block="10.0.0.0/16",
            )
        ).data

        log(f"VCN creada: {vcn.id}")

    gateways = network.list_internet_gateways(
        COMPARTMENT_ID,
        vcn_id=vcn.id,
        retry_strategy=retry,
    ).data

    if gateways:
        gateway = gateways[0]
    else:
        gateway = network.create_internet_gateway(
            oci.core.models.CreateInternetGatewayDetails(
                compartment_id=COMPARTMENT_ID,
                vcn_id=vcn.id,
                display_name="retry-ig",
                is_enabled=True,
            )
        ).data

    network.update_route_table(
        vcn.default_route_table_id,
        oci.core.models.UpdateRouteTableDetails(
            route_rules=[
                oci.core.models.RouteRule(
                    destination="0.0.0.0/0",
                    network_entity_id=gateway.id,
                )
            ]
        ),
    )

    security_lists = network.list_security_lists(
        COMPARTMENT_ID,
        vcn_id=vcn.id,
        retry_strategy=retry,
    ).data

    if security_lists:
        security_list = security_lists[0]

        ingress = list(security_list.ingress_security_rules or [])

        for port in [22, 80, 443]:
            exists = any(
                rule.protocol == "6"
                and rule.tcp_options
                and rule.tcp_options.destination_port_range
                and rule.tcp_options.destination_port_range.min == port
                and rule.tcp_options.destination_port_range.max == port
                for rule in ingress
            )

            if not exists:
                ingress.append(
                    oci.core.models.IngressSecurityRule(
                        protocol="6",
                        source="0.0.0.0/0",
                        tcp_options=oci.core.models.TcpOptions(
                            destination_port_range=oci.core.models.PortRange(
                                min=port,
                                max=port,
                            )
                        ),
                    )
                )

        network.update_security_list(
            security_list.id,
            oci.core.models.UpdateSecurityListDetails(
                ingress_security_rules=ingress,
                egress_security_rules=security_list.egress_security_rules,
            ),
        )

    subnets = network.list_subnets(
        COMPARTMENT_ID,
        vcn_id=vcn.id,
        display_name="retry-subnet",
        retry_strategy=retry,
    ).data

    if subnets:
        subnet = subnets[0]
        log(f"Usando subnet existente: {subnet.id}")
    else:
        subnet = network.create_subnet(
            oci.core.models.CreateSubnetDetails(
                compartment_id=COMPARTMENT_ID,
                vcn_id=vcn.id,
                display_name="retry-subnet",
                cidr_block="10.0.0.0/24",
                prohibit_public_ip_on_vnic=False,
            )
        ).data

        log(f"Subnet creada: {subnet.id}")

    return subnet.id


def check_existing_instance():
    compute = oci.core.ComputeClient(config)

    instances = compute.list_instances(
        COMPARTMENT_ID,
        display_name=INSTANCE_NAME,
    ).data

    for instance in instances:
        if instance.lifecycle_state not in ["TERMINATED", "TERMINATING"]:
            log(f"Ya existe una instancia: {instance.id}")
            return True

    return False


def try_create_instance(subnet_id, ad_name, image_id):
    compute = oci.core.ComputeClient(config)

    return compute.launch_instance(
        oci.core.models.LaunchInstanceDetails(
            compartment_id=COMPARTMENT_ID,
            display_name=INSTANCE_NAME,
            availability_domain=ad_name,
            shape="VM.Standard.A1.Flex",
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=ARM_OCPUS,
                memory_in_gbs=ARM_MEMORY_IN_GBS,
            ),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=image_id,
                boot_volume_size_in_gbs=BOOT_VOLUME_SIZE_IN_GBS,
            ),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=subnet_id,
                assign_public_ip=True,
            ),
            metadata={"ssh_authorized_keys": SSH_PUBLIC_KEY},
        )
    ).data


def main():
    log("Iniciando configuración de Oracle Cloud...")

    if check_existing_instance():
        log("No se creará otra instancia.")
        return

    subnet_id = create_vcn_and_subnet()

    availability_domains = get_availability_domains()
    log(f"Dominios disponibles: {availability_domains}")

    image_id = get_ubuntu_arm_image()
    log(f"Imagen Ubuntu ARM: {image_id}")

    attempt = 0

    while True:
        if check_existing_instance():
            log("La instancia ya existe. Finalizando.")
            return

        attempt += 1
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        log(f"\n[{now}] Intento #{attempt}")

        for ad_name in availability_domains:
            try:
                log(f"Probando dominio: {ad_name}")

                instance = try_create_instance(
                    subnet_id,
                    ad_name,
                    image_id,
                )

                log("¡INSTANCIA CREADA CORRECTAMENTE!")
                log(f"ID: {instance.id}")
                log(f"Estado: {instance.lifecycle_state}")
                return

            except oci.exceptions.ServiceError as error:
                message = str(error.message)

                if (
                    "capacity" in message.lower()
                    or "out of host" in message.lower()
                ):
                    log(f"Sin capacidad en {ad_name}.")
                    continue

                raise

        log(f"Sin capacidad en los dominios. Esperando {RETRY_INTERVAL} segundos.")
        time.sleep(RETRY_INTERVAL)


if __name__ == "__main__":
    main()
