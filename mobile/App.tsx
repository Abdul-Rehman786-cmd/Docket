import React, { useState } from 'react';
import { SafeAreaView, Text, Button, View, StyleSheet } from 'react-native';
import { StatusBar } from 'expo-status-bar';

const API_URL = process.env.EXPO_PUBLIC_API_URL ?? 'http://localhost:8000';

export default function App() {
  const [status, setStatus] = useState('Not checked');

  const checkHealth = async () => {
    try {
      const response = await fetch(`${API_URL}/health`);
      if (!response.ok) throw new Error('Unsuccessful request');
      const data: { status?: string } = await response.json();
      setStatus(data.status === 'ok' ? 'Backend online' : 'Unexpected response');
    } catch {
      setStatus('Cannot connect. Check API URL, network and backend.');
    }
  };

  return (
    <SafeAreaView style={styles.page}>
      <View style={styles.content}>
        <Text style={styles.heading}>Team Memory</Text>
        <Text style={styles.description}>React Native + FastAPI foundation</Text>
        <Text style={styles.status}>{status}</Text>
        <Button title="Check backend connection" onPress={checkHealth} />
        <Text style={styles.note}>Private records will appear after user authentication is implemented.</Text>
      </View>
      <StatusBar style="auto" />
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  page: { flex: 1, justifyContent: 'center', backgroundColor: '#fff' },
  content: { padding: 24, gap: 18 },
  heading: { fontSize: 30, fontWeight: '700' },
  description: { fontSize: 16 },
  status: { fontSize: 17 },
  note: { color: '#666', marginTop: 10 },
});
