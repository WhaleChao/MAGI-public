#!/usr/bin/env bash
# scripts/ops/install_quickpiper.sh
# description: Helper script to install QuickPiperAudiobook globally via Go

echo "Installing QuickPiperAudiobook via Go..."
go install github.com/C-Loftus/QuickPiperAudiobook@latest

if [ $? -eq 0 ]; then
    echo "QuickPiperAudiobook installed successfully."
    echo "The executable should be located in your GOPATH/bin (usually ~/go/bin)."
    
    # Try to add it to generic path if it's there
    if [ -f "$HOME/go/bin/QuickPiperAudiobook" ]; then
        echo "Found at $HOME/go/bin/QuickPiperAudiobook"
        # We don't modify bash_profile directly here to avoid cluttering, 
        # but the python action.py will manually check this path.
    fi
else
    echo "Installation failed. Please ensure Go is correctly installed and configured."
    exit 1
fi
