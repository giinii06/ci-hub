# GitLab to GitHub CI

GitLab이 원본입니다. GitLab Push Mirror가 보호된 `main`과 태그를 공개 GitHub 미러에 동기화합니다. CI의 빌드와 테스트는 미러의 GitHub-hosted Runner에서 실행합니다.

## ChatGPT 웹에서 CI 요청

1. ChatGPT 웹에서 GitLab 플러그인을 선택합니다.
2. 프로젝트와 브랜치를 지정해 파이프라인을 생성합니다. 예를 들어 “GitLab의 `my_streamio`에서 `ci/github-parallel-verification` 브랜치의 파이프라인을 생성하고 결과를 확인해”라고 요청합니다.
3. GitLab 파이프라인에서 `github-actions/ci-hub-request` 작업과 GitHub Actions 링크를 확인합니다. 결과 보고는 GitLab에 요청한 동일한 커밋과 파이프라인을 사용합니다.

등록된 프로젝트의 API 및 웹 요청을 GitHub에 호스팅된 컨트롤러가 5분 주기로 확인합니다. GitHub 스케줄과 러너 대기 시간에 따라 시작과 결과 반영 시각이 달라집니다. GitLab의 `github_actions_request`는 수동 표시용 작업입니다. ChatGPT에서 파이프라인을 만들면 GitHub가 빌드와 테스트를 실행합니다.

기능 브랜치 요청은 비밀값 검사를 거친 뒤 해당 커밋과 도달 가능한 이력을 공개 미러에 게시합니다. Rust 요청에서 HLS 통합 테스트가 있는 경우 빌드·테스트, Clippy, HLS는 독립 작업으로 실행합니다.

## 준비

`gh`, `glab`, `git`, Python 3.9 이상, Gitleaks를 설치합니다. `gh auth login`과 `glab auth login --hostname gitlab.com`을 실행합니다. GitLab 저장소를 SSH로 읽을 수 있어야 합니다. GitHub는 로그인한 개인 계정을 대상으로 합니다.

```sh
mkdir -p ~/.config/ci-hub
chmod 700 ~/.config/ci-hub
cp projects.example.json ~/.config/ci-hub/projects.json
```

`github_owner`와 `gitlab_namespace`를 설정합니다. 별도 계정에서 중앙 워크플로를 운영하려면 공개 `ci-hub` 저장소를 만들고 이 저장소의 워크플로를 `v1` 브랜치에 게시합니다.

## 프로젝트 연결

1. `python3 bridge.py discover`로 비공개 프로젝트 ID를 확인합니다.
2. `python3 bridge.py prepare PROJECT_ID`를 실행합니다. 매니페스트가 여럿 있으면 `--stack bun`, `--stack go`, `--stack rust` 가운데 하나를 지정합니다. 모듈이 하위 디렉터리에 있으면 `--directory`로 경로를 지정합니다.
3. 출력된 GitLab MR을 검토하고 병합합니다. GitHub에서 GitLab으로 코드나 태그를 보내는 기존 워크플로와 Pull Mirror를 해제합니다. 같은 이름의 기존 GitHub 저장소가 있으면 새 미러 이름을 사용합니다. GitLab에서 이 프로젝트의 유일한 보호 브랜치를 `main`으로 유지합니다.
4. `python3 bridge.py scan PROJECT_ID`를 실행합니다. `main`과 태그에서 도달할 수 있는 이력, 기존 GitHub Actions 워크플로를 검토합니다. 연결 후에는 새 `main` 커밋과 태그도 자동 공개됩니다. 다른 브랜치에 붙인 태그도 공개 대상입니다.
5. 검토한 SHA와 프로젝트를 로컬 설정의 `approved` 배열에 추가합니다. `tags`에는 스캔 명령이 출력한 태그와 Git 객체 SHA를 넣습니다.

   ```json
   {
     "id": 12345,
     "name": "example-project",
     "sha": "0123456789012345678901234567890123456789",
     "stack": "go",
     "directory": ".",
     "tags": {}
   }
   ```

6. `python3 bridge.py tick`을 실행합니다. 최초 연결 시 승인 SHA, 태그, 호출 파일을 확인하고 커밋 이력을 검사합니다. GitHub 공개 저장소와 해당 저장소 전용 SSH deploy key를 만들고 GitLab 미러를 활성화합니다.

Gitleaks 오탐을 확인했으면 해당 프로젝트의 로컬 설정에 `gitleaks_ignore_fingerprints` 배열을 추가합니다. 각 항목은 검토한 탐지 결과의 전체 지문입니다.

README만 있는 프로젝트에는 `prepare PROJECT_ID --stack docs`를 사용합니다. 이 설정은 README의 공백 오류와 충돌 표시를 검사합니다. 소스 코드를 병합할 때 호출 워크플로의 `stack` 값을 해당 빌드 환경으로 바꿉니다.

GitLab 저장소에서 연결을 변경하려면 **Settings > Repository > Mirroring repositories**를 엽니다. 스케줄 전에 `approved`에서 프로젝트를 제거하면 브리지가 상태 보고를 중지합니다. GitLab Push Mirror는 별도로 비활성화해야 합니다. 이미 공개된 커밋은 복제본에 남을 수 있습니다.

## 운영

GitLab 프로젝트의 CI 구성 경로는 공통 요청 템플릿을 가리킵니다. GitHub 공개 미러에는 `gitlab-request.yml` 진입 워크플로가 필요합니다. GitLab 프로젝트별 토큰은 비공개 컨트롤러의 Actions Secrets에서 관리합니다. 새 프로젝트는 이 컨트롤러에 등록합니다.

로컬 `bridge.py discover`, `prepare`, `scan`, `reconcile`은 미러 초기 설정과 수동 진단에 사용할 수 있습니다.

공통 CI를 갱신하려면 중앙 저장소의 `v1` 브랜치를 업데이트합니다. 각 프로젝트의 호출 파일은 동일한 `v1`을 참조합니다.

Bun 테스트에 FFmpeg가 필요한 저장소에는 `gh variable set CI_HUB_FFMPEG --body true -R OWNER/MIRROR`를 실행합니다.
