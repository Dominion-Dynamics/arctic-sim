# SITL CONTAINER — ArduPilot software-in-the-loop, multi-vehicle.
#
# Builds the vehicle firmwares listed in VEHICLES. Each is a separate waf
# target, so building all four roughly quadruples image build time; trim the
# list if you only fly one.
#
# Talks to Gazebo over the ArduPilotPlugin UDP channel (9002 in / 9003 out),
# not through ROS or MAVLink. MAVLink is only for ground stations.
FROM ubuntu:22.04

# 5760 = MAVLink TCP (SITL), 14550/14551 = MAVProxy UDP out to GCS
EXPOSE 5760 14550/udp 14551/udp

ARG VEHICLES="copter rover plane antennatracker"
ARG ARDUPILOT_TAG=Copter-4.5
ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y --no-install-recommends \
        sudo git python3 python3-pip python3-dev lsb-release tzdata \
        net-tools socat ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir MAVProxy pymavlink

# ArduPilot's build refuses to run as root.
ARG USER_NAME=ardupilot
ARG USER_UID=1000
RUN groupadd ${USER_NAME} --gid ${USER_UID} && \
    useradd -l -m ${USER_NAME} -u ${USER_UID} -g ${USER_UID} -s /bin/bash && \
    echo "${USER_NAME} ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/${USER_NAME} && \
    chmod 0440 /etc/sudoers.d/${USER_NAME}

USER ${USER_NAME}
WORKDIR /home/${USER_NAME}
ENV USER=${USER_NAME}

RUN git config --global url.https://github.com/.insteadOf git://github.com/ && \
    git clone --branch ${ARDUPILOT_TAG} --depth 1 \
        https://github.com/ArduPilot/ardupilot.git ardupilot

WORKDIR /home/${USER_NAME}/ardupilot
RUN git submodule update --init --recursive --depth 1

# Skip the interactive/graphical extras; we only need the SITL toolchain.
ENV SKIP_AP_EXT_ENV=1 SKIP_AP_GRAPHIC_ENV=1 SKIP_AP_COV_ENV=1 SKIP_AP_GIT_CHECK=1
RUN Tools/environment_install/install-prereqs-ubuntu.sh -y

ENV PATH=/home/${USER_NAME}/.local/bin:/home/${USER_NAME}/ardupilot/Tools/autotest:${PATH}

RUN ./waf configure --board sitl && \
    for v in ${VEHICLES}; do echo "=== building $v ==="; ./waf $v; done

COPY --chown=${USER_NAME}:${USER_NAME} sitl/params /params
COPY --chown=${USER_NAME}:${USER_NAME} sitl/entrypoint.sh /entrypoint.sh
USER root
RUN chmod +x /entrypoint.sh
USER ${USER_NAME}

ENTRYPOINT ["/entrypoint.sh"]
