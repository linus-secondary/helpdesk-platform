// CI/CD pipeline: test -> scan -> build -> push to Amazon ECR -> deploy to Amazon EKS
// Runs on the Jenkins server from jenkins/ (has Docker, AWS CLI, kubectl, Trivy, Checkov).
// Jenkins credential needed: "aws-credentials" (AWS access key, kind "AWS Credentials" or
// username/password). Pushes to the main branch deploy; other branches only test and scan.
pipeline {
  agent any

  options {
    timestamps()
    timeout(time: 45, unit: 'MINUTES')
    buildDiscarder(logRotator(numToKeepStr: '20'))
    disableConcurrentBuilds()
  }

  environment {
    AWS_REGION   = 'us-east-1'
    APP_NAME     = 'helpdesk-platform'
    IMAGE_TAG    = "${env.GIT_COMMIT ? env.GIT_COMMIT.take(7) : env.BUILD_NUMBER}"
    LOCAL_IMAGE  = "${APP_NAME}:${IMAGE_TAG}"
  }

  stages {
    stage('Unit tests') {
      steps {
        sh '''
          python3 -m venv .venv
          . .venv/bin/activate
          pip install -q -r app/requirements.txt pytest ruff cfn-lint
          ruff check app tests
          pytest -q --junitxml=reports/junit.xml tests
        '''
      }
      post { always { junit allowEmptyResults: true, testResults: 'reports/junit.xml' } }
    }

    stage('IaC security scan') {
      steps {
        // Checkov scans Terraform, Kubernetes and CloudFormation for security misconfigurations;
        // cfn-lint validates the CloudFormation template against the AWS resource specification
        sh 'checkov -d terraform --framework terraform --quiet --compact --soft-fail'
        sh 'checkov -d k8s --framework kubernetes --quiet --compact --soft-fail'
        sh '.venv/bin/cfn-lint cloudformation/*.yaml'
        sh 'checkov -d cloudformation --framework cloudformation --quiet --compact --soft-fail'
      }
    }

    stage('Build image') {
      steps {
        sh 'docker build -t ${LOCAL_IMAGE} app'
      }
    }

    stage('Image vulnerability scan') {
      steps {
        // Fail the build on CRITICAL vulnerabilities that already have a fix
        sh 'trivy image --exit-code 1 --severity CRITICAL --ignore-unfixed --no-progress ${LOCAL_IMAGE}'
      }
    }

    stage('Push to ECR') {
      when { branch 'main' }
      steps {
        withCredentials([[$class: 'AmazonWebServicesCredentialsBinding', credentialsId: 'aws-credentials']]) {
          sh '''
            ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
            REGISTRY=${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com
            aws ecr get-login-password --region ${AWS_REGION} | docker login --username AWS --password-stdin ${REGISTRY}
            docker tag ${LOCAL_IMAGE} ${REGISTRY}/${APP_NAME}:${IMAGE_TAG}
            docker tag ${LOCAL_IMAGE} ${REGISTRY}/${APP_NAME}:latest
            docker push ${REGISTRY}/${APP_NAME}:${IMAGE_TAG}
            docker push ${REGISTRY}/${APP_NAME}:latest
          '''
        }
      }
    }

    stage('Deploy to EKS') {
      when { branch 'main' }
      steps {
        withCredentials([[$class: 'AmazonWebServicesCredentialsBinding', credentialsId: 'aws-credentials']]) {
          sh 'IMAGE_TAG=${IMAGE_TAG} bash scripts/aws-deploy.sh'
        }
      }
    }

    stage('Smoke test') {
      when { branch 'main' }
      steps {
        withCredentials([[$class: 'AmazonWebServicesCredentialsBinding', credentialsId: 'aws-credentials']]) {
          sh '''
            URL=$(kubectl get ingress helpdesk -n helpdesk -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
            . .venv/bin/activate
            python scripts/smoke_test.py "http://${URL}"
          '''
        }
      }
    }
  }

  post {
    always {
      sh 'docker image rm ${LOCAL_IMAGE} || true'
      cleanWs(deleteDirs: true, notFailBuild: true)
    }
    failure { echo 'Pipeline failed: check the stage logs above.' }
  }
}
