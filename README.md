# GitLab to GitHub CI

GitLab이 원본입니다. 공개가 승인된 프로젝트마다 GitHub 공개 저장소를 만들고, GitLab Push Mirror가 보호된 `main`을 전달합니다. GitHub Actions는 중앙 `ci.yml@v1`로 빌드와 테스트를 실행합니다. 로컬 브리지가 `glab`으로 같은 SHA의 GitLab Commit Status를 기록합니다.

## 준비

`gh`, `glab`, `git`, Python 3, Gitleaks를 설치합니다. `gh auth login`과 `glab auth login --hostname gitlab.com`을 실행합니다. GitLab 저장소를 SSH로 읽을 수 있어야 합니다. GitHub는 로그인한 개인 계정을 대상으로 합니다.

```sh
mkdir -p ~/.config/ci-hub
chmod 700 ~/.config/ci-hub
cp projects.example.json ~/.config/ci-hub/projects.json
```

`github_owner`와 `gitlab_namespace`를 설정합니다. 별도 계정에서 중앙 워크플로를 운영하려면 공개 `ci-hub` 저장소를 만들고 이 저장소의 워크플로를 `v1` 브랜치에 게시합니다.

## 프로젝트 연결

1. `python3 bridge.py discover`로 비공개 프로젝트 ID를 확인합니다.
2. `python3 bridge.py prepare PROJECT_ID`를 실행합니다. 매니페스트가 여럿 있으면 `--stack bun`, `--stack go`, `--stack rust` 가운데 하나를 지정합니다. 모듈이 하위 디렉터리에 있으면 `--directory`로 경로를 지정합니다.
3. 출력된 GitLab MR을 검토하고 병합합니다. GitLab에서 이 프로젝트의 유일한 보호 브랜치를 `main`으로 유지합니다.
4. `python3 bridge.py scan PROJECT_ID`를 실행합니다. 이 시점의 `main`에서 도달 가능한 이력과 기존 GitHub Actions 워크플로를 공개해도 되는지 검토합니다. 이후 `main`에 들어가는 커밋도 자동으로 공개됩니다.
5. 검토한 SHA와 프로젝트를 로컬 설정의 `approved` 배열에 추가합니다.

   ```json
   {
     "id": 12345,
     "name": "example-project",
     "sha": "0123456789012345678901234567890123456789",
     "stack": "go",
     "directory": "."
   }
   ```

6. `python3 bridge.py tick`을 실행합니다. 최초 연결 시 승인 SHA와 호출 파일을 확인하고 커밋 이력을 검사합니다. GitHub 공개 저장소와 해당 저장소 전용 SSH deploy key를 만들고 GitLab 미러를 활성화합니다.

GitLab 저장소에서 연결을 변경하려면 **Settings > Repository > Mirroring repositories**를 엽니다. 스케줄 전에 `approved`에서 프로젝트를 제거하면 브리지가 상태 보고를 중지합니다. GitLab Push Mirror는 별도로 비활성화해야 합니다. 이미 공개된 커밋은 복제본에 남을 수 있습니다.

## 주기 실행

`python3 bridge.py tick`을 로그인 계정으로 5분마다 실행하도록 예약합니다. `discovered.json`에서 신규 프로젝트를 확인할 수 있습니다. 실제 동기화는 GitLab이 실행합니다. 결과 보고는 이 계정의 스케줄러가 실행 중일 때 동작하며, 회차마다 각 저장소의 최근 완료 실행 최대 50건을 조회합니다. 연결된 저장소에 GitLab API 토큰을 등록할 필요는 없습니다.

공통 CI를 갱신하려면 중앙 저장소의 `v1` 브랜치를 업데이트합니다. 각 프로젝트의 호출 파일은 동일한 `v1`을 참조합니다.
