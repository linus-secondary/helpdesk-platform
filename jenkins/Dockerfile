# Jenkins controller with the tools the pipeline needs: Docker CLI, AWS CLI, kubectl, Trivy, Checkov, Python.
FROM jenkins/jenkins:2.479.1-lts-jdk17

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl unzip gnupg python3 python3-venv python3-pip docker.io \
    && rm -rf /var/lib/apt/lists/*

# AWS CLI v2
RUN curl -sSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscli.zip \
    && unzip -q /tmp/awscli.zip -d /tmp && /tmp/aws/install && rm -rf /tmp/aws /tmp/awscli.zip

# kubectl (matches the EKS version in terraform/variables.tf)
RUN curl -sSL "https://dl.k8s.io/release/v1.31.2/bin/linux/amd64/kubectl" -o /usr/local/bin/kubectl \
    && chmod +x /usr/local/bin/kubectl

# Trivy (image vulnerability scanner)
RUN curl -sSfL https://raw.githubusercontent.com/aquasecurity/trivy/v0.57.1/contrib/install.sh | sh -s -- -b /usr/local/bin v0.57.1

# Checkov (infrastructure-as-code scanner)
RUN pip3 install --break-system-packages --no-cache-dir checkov==3.2.301

# Let the jenkins user talk to the host's Docker engine through the mounted socket
RUN usermod -aG docker jenkins

# Plugins used by the Jenkinsfile
RUN jenkins-plugin-cli --plugins \
      workflow-aggregator git github-branch-source pipeline-stage-view \
      aws-credentials pipeline-aws junit timestamper ws-cleanup

USER jenkins
