FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV USER=user
ENV HOME=/home/$USER
ENV DISPLAY=:1

# Base + apps
RUN apt-get update && apt-get install -y \
    xfce4 \
    xfce4-terminal \
    tigervnc-standalone-server \
    tigervnc-common \
    nano \
    dbus-x11 \
    dbus-user-session \
    x11-xserver-utils \
    wget \
    curl \
    git \
    tor \
    sudo \
    python3-pip \
    python-is-python3 \
    libreoffice-writer \
    libreoffice-calc \
    vlc \
    mpv \
    gnupg \
    ca-certificates \
    iproute2 \
    net-tools \
    iputils-ping \
    && wget -q -O /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y /tmp/google-chrome.deb \
    && rm /tmp/google-chrome.deb \
    && apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Extra dev tools and apps (NodeJS, Docker, GIMP, etc.)
RUN apt-get update && apt-get install -y \
    gimp \
    copyq \
    jq \
    # Setup NodeSource repo for NodeJS
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    # Setup Docker repo
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null \
    # Install NodeJS, Docker CLI and download yq
    && apt-get update \
    && apt-get install -y nodejs docker-ce-cli \
    && wget https://github.com/mikefarah/yq/releases/latest/download/yq_linux_amd64 -O /usr/bin/yq && chmod +x /usr/bin/yq \
    && apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Configure Chrome default search engine to Brave
RUN mkdir -p /etc/opt/chrome/policies/managed && \
    printf '%s\n' \
'{' \
'  "DefaultSearchProviderEnabled": true,' \
'  "DefaultSearchProviderName": "Brave Search",' \
'  "DefaultSearchProviderKeyword": "brave.com",' \
'  "DefaultSearchProviderSearchURL": "https://search.brave.com/search?q={searchTerms}",' \
'  "DefaultSearchProviderSuggestURL": "https://search.brave.com/api/suggest?q={searchTerms}",' \
'  "DefaultSearchProviderIconURL": "https://search.brave.com/favicon.ico"' \
'}' \
    > /etc/opt/chrome/policies/managed/default_search.json

# VSCode
RUN wget -qO- https://packages.microsoft.com/keys/microsoft.asc | gpg --dearmor > /usr/share/keyrings/packages.microsoft.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/packages.microsoft.gpg] https://packages.microsoft.com/repos/code stable main" > /etc/apt/sources.list.d/vscode.list && \
    apt-get update && apt-get install -y code && \
    apt-get clean && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# User
RUN useradd -m -s /bin/bash -G sudo $USER && \
    # Donner l'accès sudo sans mot de passe, mais garder le compte utilisateur sans mot de passe et verrouillé
    echo "$USER ALL=(ALL) NOPASSWD:ALL" >> /etc/sudoers && \
    passwd -d $USER && \
    passwd -l $USER && \
    update-alternatives --install /usr/bin/x-www-browser x-www-browser /usr/bin/google-chrome 200 && \
    update-alternatives --set x-www-browser /usr/bin/google-chrome

USER $USER
WORKDIR $HOME

# xstartup robuste: pas de veille/écran noir, session via dbus
RUN mkdir -p $HOME/.vnc && \
    printf '%s\n' \
'#!/bin/sh' \
'unset SESSION_MANAGER' \
'unset DBUS_SESSION_BUS_ADDRESS' \
'export DISPLAY=:1' \
'xset -dpms' \
'xset s off' \
'xset s noblank' \
'if command -v dbus-run-session >/dev/null 2>&1; then' \
'  exec dbus-run-session -- startxfce4' \
'else' \
'  exec dbus-launch --exit-with-session startxfce4' \
'fi' \
    > $HOME/.vnc/xstartup && \
    chmod +x $HOME/.vnc/xstartup

# XFCE defaults (fond d'écran noir + pas d'icônes système)
RUN mkdir -p $HOME/.config/xfce4/xfconf/xfce-perchannel-xml && \
    printf '%s\n' \
'<?xml version="1.0" encoding="UTF-8"?>' \
'<channel name="xfce4-desktop" version="1.0">' \
'  <property name="backdrop" type="empty">' \
'    <property name="screen0" type="empty">' \
'      <property name="monitorVNC-0" type="empty">' \
'        <property name="workspace0" type="empty">' \
'          <property name="color-style" type="int" value="0"/>' \
'          <property name="image-style" type="int" value="0"/>' \
'          <property name="rgba1" type="array">' \
'            <value type="double" value="0.0"/>' \
'            <value type="double" value="0.0"/>' \
'            <value type="double" value="0.0"/>' \
'            <value type="double" value="1.0"/>' \
'          </property>' \
'        </property>' \
'      </property>' \
'    </property>' \
'  </property>' \
'  <property name="desktop-icons" type="empty">' \
'    <property name="file-icons" type="empty">' \
'      <property name="show-home" type="bool" value="false"/>' \
'      <property name="show-filesystem" type="bool" value="false"/>' \
'      <property name="show-removable" type="bool" value="false"/>' \
'      <property name="show-trash" type="bool" value="false"/>' \
'    </property>' \
'  </property>' \
'</channel>' \
    > $HOME/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml

# Autostart (remove panel2)
RUN mkdir -p $HOME/.config/xfce4/panel && \
    printf '%s\n' \
'[xfce4-panel]' \
'plugin-14=actions' \
'[plugin-14]' \
'appearance=0' \
'items=-lock-screen;-switch-user;-separator;-suspend;-hibernate;-hybrid-sleep;-shutdown;-restart;-logout;-logout-dialog' \
    > $HOME/.config/xfce4/panel/actions.rc

RUN mkdir -p $HOME/.config/xfce4/xfconf/xfce-perchannel-xml && \
    printf '%s\n' \
'<?xml version="1.0" encoding="UTF-8"?>' \
'<channel name="xfce4-session" version="1.0">' \
'  <property name="shutdown" type="empty">' \
'    <property name="ShowHibernate" type="bool" value="false"/>' \
'    <property name="ShowSuspend" type="bool" value="false"/>' \
'  </property>' \
'</channel>' \
    > $HOME/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-session.xml

USER root

# Remplacer xflock4 par un script vide et purger les lockers
RUN apt-get purge -y light-locker xscreensaver* || true && \
    echo "#!/bin/true" > /usr/bin/xflock4 && \
    chmod +x /usr/bin/xflock4

USER $USER

RUN mkdir -p $HOME/.config/autostart && \
    printf '%s\n' \
'#!/bin/bash' \
'sleep 5' \
'export DISPLAY=:1' \
'xfconf-query -c xfce4-panel -p /panels/panel-2 -r -R' \
'xfconf-query -c xfce4-panel -p /panels -t int -s 1 -a' \
'xfce4-panel -r' \
'rm -f ~/.config/autostart/remove-panel2.desktop' \
    > $HOME/.config/autostart/remove-panel2.sh && \
    chmod +x $HOME/.config/autostart/remove-panel2.sh && \
    printf '%s\n' \
'[Desktop Entry]' 'Type=Application' 'Name=Remove Panel2' \
'Exec=/home/user/.config/autostart/remove-panel2.sh' \
'X-GNOME-Autostart-enabled=true' \
    > $HOME/.config/autostart/remove-panel2.desktop

# Raccourcis bureau + création du répertoire Desktop
RUN mkdir -p $HOME/Desktop && \
    printf '%s\n' \
"[Desktop Entry]" "Version=1.0" "Type=Application" "Name=Chrome" \
"Exec=/usr/bin/google-chrome --disable-dev-shm-usage --disable-software-rasterizer %U" \
"Icon=google-chrome" "Terminal=false" "Categories=Network;" \
"MimeType=text/html;text/xml;application/xhtml+xml;x-scheme-handler/http;x-scheme-handler/https;" \
    > $HOME/Desktop/chrome.desktop && \
    printf '%s\n' \
"[Desktop Entry]" "Version=1.0" "Type=Application" "Name=VSCode" \
"Exec=/usr/bin/code --no-sandbox --unity-launch --disable-gpu %F" \
"Icon=vscode" "Terminal=false" "Categories=Development;" "MimeType=text/plain;" \
    > $HOME/Desktop/vscode.desktop && \
    printf '%s\n' \
"[Desktop Entry]" "Version=1.0" "Type=Application" "Name=LibreOffice" \
"Exec=libreoffice %U" "Icon=libreoffice-startcenter" "Terminal=false" "Categories=Office;" \
    > $HOME/Desktop/libreoffice.desktop && \
    printf '%s\n' \
"[Desktop Entry]" "Version=1.0" "Type=Application" "Name=VLC" \
"Exec=vlc %U" "Icon=vlc" "Terminal=false" "Categories=AudioVideo;" \
    > $HOME/Desktop/vlc.desktop && \
    printf '%s\n' \
"[Desktop Entry]" "Version=1.0" "Type=Application" "Name=Terminal" \
"Exec=xfce4-terminal" "Icon=utilities-terminal" "Terminal=false" "Categories=System;" \
    > $HOME/Desktop/terminal.desktop && \
    printf '%s\n' \
"[Desktop Entry]" "Version=1.0" "Type=Application" "Name=Files" \
"Exec=thunar" "Icon=system-file-manager" "Terminal=false" "Categories=System;" \
    > $HOME/Desktop/files.desktop && \
    chmod +x $HOME/Desktop/*.desktop


RUN sudo passwd -d $USER

USER root

# Configurer DNS et réseau pour Internet
RUN mkdir -p /etc/docker && \
    echo "nameserver 8.8.8.8" > /etc/resolv.conf && \
    echo "nameserver 8.8.4.4" >> /etc/resolv.conf && \
    echo "nameserver 1.1.1.1" >> /etc/resolv.conf

# Entrypoint: FQDN + préparation Desktop + password optionnel + flag INSEC si None + attente + logs
RUN ["/bin/bash", "-lc", "cat >/usr/local/bin/entrypoint.sh << 'EOF'\n#!/bin/bash\nset -euo pipefail\nshopt -s nullglob\n\nPORT=5901\nUSER=user\nHOME=/home/$USER\nexport HOME DISPLAY\n\n# Nettoyage locks X\nrm -f /tmp/.X*-lock || true\nrm -f /tmp/.X11-unix/X* || true\nmkdir -p /tmp /tmp/.X11-unix && chmod 1777 /tmp /tmp/.X11-unix || true\n\n# Hostname résolvable (FQDN)\ngrep -qE '^127\\.0\\.0\\.1\\s+localhost' /etc/hosts || echo '127.0.0.1 localhost' >> /etc/hosts\nHOST=$(hostname -s 2>/dev/null || hostname)\nFQDN=\"${HOST}.localdomain\"\nif ! hostname -f >/dev/null 2>&1; then\n  sed -i '/^127\\.0\\.1\\.1/d' /etc/hosts\n  echo \"127.0.1.1 $FQDN $HOST\" >> /etc/hosts\nfi\n\n# Fichier Xauthority (pour supprimer l'avertissement)\nsu - $USER -c 'touch ~/.Xauthority' || true\n\n# Préparer les icônes sur le bureau à chaque démarrage\nsu - $USER -c 'mkdir -p ~/Desktop && cp -u /tmp/desktop-defaults/*.desktop ~/Desktop/ 2>/dev/null || true && chmod +x ~/Desktop/*.desktop 2>/dev/null || true'\n\n# First run: copier configs XFCE\nif [ ! -f \"$HOME/.config/.initialized\" ]; then\n    echo \"First run - copying default configs...\"\n    mkdir -p \"$HOME/.config/xfce4/xfconf/xfce-perchannel-xml\" \"$HOME/.config/autostart\"\n    cp -n /tmp/xfce4-defaults/xfce4-desktop.xml \"$HOME/.config/xfce4/xfconf/xfce-perchannel-xml/\" 2>/dev/null || true\n    chown -R $USER:$USER \"$HOME/.config\"\n    touch \"$HOME/.config/.initialized\"\nfi\n\n# Security types: None par défaut; VncAuth si demandé par env\nSEC_TYPES=\"${VNC_SECURITY_TYPES:-None}\"\nif [[ \"$SEC_TYPES\" = \"VncAuth\" ]]; then\n  if [[ -n \"${VNC_PASSWORD:-}\" ]]; then\n      su - $USER -c \"mkdir -p ~/.vnc && umask 077 && echo \\\"$VNC_PASSWORD\\\" | vncpasswd -f > ~/.vnc/passwd && chmod 600 ~/.vnc/passwd\"\n  elif [[ ! -f \"$HOME/.vnc/passwd\" ]]; then\n      echo \"VNC_SECURITY_TYPES=VncAuth but no VNC_PASSWORD provided and no existing passwd file.\" >&2\n      echo \"Provide VNC_PASSWORD or start with no password (VNC_SECURITY_TYPES=None).\" >&2\n      exit 1\n  fi\nfi\n\n# Flag explicite si pas de mot de passe\nif [[ \"$SEC_TYPES\" = \"None\" ]]; then\n  INSEC=\"--I-KNOW-THIS-IS-INSECURE\"\nelse\n  INSEC=\"\"\nfi\n\n# Stop ancienne session\nsu - $USER -c \"vncserver -kill :1 >/dev/null 2>&1 || true\"\n\n# Démarre VNC (:1 -> 5901)\nsu - $USER -c \"vncserver :1 -localhost no -geometry 1920x1080 -depth 24 -SecurityTypes ${SEC_TYPES} -rfbport ${PORT} ${INSEC}\"\n\necho \"Waiting for VNC to listen on port ${PORT}...\"\nfor i in $(seq 1 60); do\n  if ss -ltn \"( sport = :$PORT )\" | grep -q LISTEN; then\n    echo \"VNC is ready on port ${PORT}\"\n    break\n  fi\n  sleep 0.5\ndone\n\n# Logs\nlogs=($HOME/.vnc/*:1.log)\nif (( ${#logs[@]} > 0 )); then\n  exec tail -F \"${logs[@]}\"\nelse\n  exec tail -F $HOME/.vnc/*.log\nfi\nEOF\nchmod +x /usr/local/bin/entrypoint.sh"]


# Defaults à copier au premier run
RUN mkdir -p /tmp/xfce4-defaults && \
    cp /home/user/.config/xfce4/xfconf/xfce-perchannel-xml/xfce4-desktop.xml /tmp/xfce4-defaults/ || true

EXPOSE 5901
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]