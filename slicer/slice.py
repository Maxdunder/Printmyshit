name: Slice
 
on:
  workflow_dispatch:
    inputs:
      model:
        description: "Path of the model in the repo (incoming/...)"
        required: true
      material:
        description: "PLA, PETG, ABS or TPU"
        required: true
        default: "PLA"
      layer:
        description: "Layer height in mm"
        required: true
        default: "0.20"
      infill:
        description: "Infill in percent"
        required: true
        default: "15"
 
permissions:
  contents: write
 
concurrency:
  group: slice
  cancel-in-progress: false
 
jobs:
  slice:
    runs-on: ubuntu-24.04
    timeout-minutes: 30
    env:
      MODEL: ${{ inputs.model }}
      MATERIAL: ${{ inputs.material }}
      LAYER: ${{ inputs.layer }}
      INFILL: ${{ inputs.infill }}
      GH_TOKEN: ${{ github.token }}
    steps:
      - uses: actions/checkout@v4
 
      - name: Install system libraries
        run: |
          sudo apt-get update
          sudo apt-get install -y xvfb libgtk-3-0 libwebkit2gtk-4.1-0 libglu1-mesa libegl1 libgl1 libosmesa6
      - name: Download OrcaSlicer
        run: |
          arch="$(uname -m)"
          case "$arch" in
            x86_64)
              pattern="x86_64"
              ;;
            arm64|aarch64)
              pattern="aarch64"
              ;;
            *)
              echo "Unsupported architecture: $arch"
              exit 1
              ;;
          esac

          url=$(gh api repos/SoftFever/OrcaSlicer/releases/latest \
            --jq --arg pattern "$pattern" '.assets[].browser_download_url | select(test("AppImage$")) | select(test("Ubuntu2404")) | select(test($pattern))' | head -n 1)

          if [ -z "$url" ]; then
            echo "No OrcaSlicer AppImage found for architecture $arch"
            exit 1
          fi

          echo "Using $url"
          curl -fsSL "$url" -o orca.AppImage
          chmod +x orca.AppImage
          file ./orca.AppImage
          ./orca.AppImage --appimage-extract > /dev/null
          ls squashfs-root | head -30
 
      - name: Slice
        run: python3 slicer/slice.py
 
      - name: Commit the sliced file
        run: |
          git config user.name "github-actions"
          git config user.email "github-actions@users.noreply.github.com"
          git add models/
          git rm -q --ignore-unmatch -- "$MODEL"
          git commit -m "Sliced $(basename "$MODEL") for $MATERIAL" || echo "nothing to commit"
          git pull --rebase
          git push
